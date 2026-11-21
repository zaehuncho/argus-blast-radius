"""Team Argus - Blast Radius selector.

For each patch against the pinned sqlglot commit, picks the tests that can fail
and packs them onto workers. Selection is static:
  * Python source: compare the code tree before and after the patch. A change
    that leaves the tree identical (comments, blank lines) can break nothing.
    Otherwise find the smallest changed statements. Inside a function body, the
    tests are the ones whose recorded coverage runs those lines. At module or
    class level (executed at import), the tests are every test that runs any
    line of that module.
  * Test files: the tests defined in the changed part, or the whole file for
    module-level changes; helpers map through coverage of the tests/ tree.
  * Data files under tests/: the tests that opened them during the map build.
Anything we cannot reason about selects every test, since a miss costs 300 s.
"""
import ast
import json
import os
import re
import sqlite3
import sys
import zlib

DATA = os.environ.get("ARGUS_DATA", "/data")
SRC = os.environ.get("ARGUS_SQLGLOT", "/app/sqlglot")
MAP = os.environ.get("ARGUS_MAP", "/app/map/cov.sqlite")
WORKER_COST_MS = 2000
MAX_WORKERS = 8


def log(*a):
    print(*a, file=sys.stderr)


# ---------------------------------------------------------------- patches

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_patch(text):
    """Returns a list of {old, new, hunks}; each hunk is (old_start, lines)."""
    files, cur = [], None
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            cur = {"old": None, "new": None, "hunks": []}
            files.append(cur)
            m = re.match(r"diff --git a/(.*) b/(.*)$", line)
            if m:
                cur["old"], cur["new"] = m.group(1), m.group(2)
        elif line.startswith("--- ") and cur is not None and not cur["hunks"]:
            p = line[4:].split("\t")[0]
            cur["old"] = None if p == "/dev/null" else re.sub(r"^a/", "", p)
        elif line.startswith("+++ ") and cur is not None and not cur["hunks"]:
            p = line[4:].split("\t")[0]
            cur["new"] = None if p == "/dev/null" else re.sub(r"^b/", "", p)
        elif line.startswith("rename from ") and cur is not None:
            cur["old"] = line[len("rename from "):]
        elif line.startswith("rename to ") and cur is not None:
            cur["new"] = line[len("rename to "):]
        elif line.startswith("@@") and cur is not None:
            m = HUNK.match(line)
            old_start = int(m.group(1))
            old_len = int(m.group(2)) if m.group(2) is not None else 1
            new_len = int(m.group(4)) if m.group(4) is not None else 1
            body = []
            i += 1
            o = n = 0
            while i < len(lines) and (o < old_len or n < new_len):
                l = lines[i]
                if l.startswith("\\"):
                    i += 1
                    continue
                tag = l[:1] if l else " "
                body.append((tag, l[1:]))
                if tag in (" ", "-"):
                    o += 1
                if tag in (" ", "+"):
                    n += 1
                i += 1
            cur["hunks"].append((old_start, body))
            continue
        i += 1
    return files


class PatchMismatch(Exception):
    """The patch's context or removed lines do not match the pinned file."""


def apply_hunks(old_text, hunks):
    """Applies hunks to old_text. Returns (new_text, changed_old_lines).

    Raises PatchMismatch if any context or removed line differs from the file,
    so a misapplied patch can never look like 'no change'.
    """
    old = old_text.split("\n")
    out, changed = [], set()
    pos = 0  # 0-based index into old
    edits = 0
    for old_start, body in hunks:
        start = max(old_start - 1, 0)
        if start < pos:
            raise PatchMismatch("overlapping hunks")
        out.extend(old[pos:start])
        o = start
        for tag, text in body:
            if tag == " ":
                if o >= len(old) or old[o].rstrip("\r") != text.rstrip("\r"):
                    raise PatchMismatch(f"context differs at line {o + 1}")
                out.append(old[o])
                o += 1
            elif tag == "-":
                if o >= len(old) or old[o].rstrip("\r") != text.rstrip("\r"):
                    raise PatchMismatch(f"removed line differs at line {o + 1}")
                changed.add(o + 1)
                o += 1
                edits += 1
            elif tag == "+":
                out.append(text)
                # an insertion sits between old lines o and o+1 (1-based)
                changed.add(max(o, 1))
                changed.add(o + 1)
                edits += 1
        pos = o
    out.extend(old[pos:])
    if not edits:
        raise PatchMismatch("hunk without edits")
    return "\n".join(out), changed


# ---------------------------------------------------------------- map

class Map:
    def __init__(self, path):
        self.db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        self.files = {p: i for i, p in self.db.execute("select id, path from files")}
        self._cache = {}
        self._blobs = {}

    def blob(self, bid):
        if bid not in self._blobs:
            (b,) = self.db.execute("select bits from blobs where id=?", (bid,)).fetchone()
            self._blobs[bid] = zlib.decompress(b)
        return self._blobs[bid]

    def lines(self, path):
        """{test: bits} for one source file."""
        if path not in self._cache:
            fid = self.files.get(path)
            rows = {}
            if fid is not None:
                for t, bid in self.db.execute("select test, blob_id from lines where file_id=?", (fid,)):
                    rows[t] = self.blob(bid)
            self._cache[path] = rows
        return self._cache[path]

    def tests_covering(self, path, linenos):
        linenos = list(linenos)
        res = set()
        for t, bits in self.lines(path).items():
            n = len(bits)
            for l in linenos:
                b = l >> 3
                if b < n and bits[b] & (1 << (l & 7)):
                    res.add(t)
                    break
        return res

    def tests_touching_file(self, path):
        return {t for t, bits in self.lines(path).items() if any(bits)}

    def tests_opening(self, path):
        res = {t for (t,) in self.db.execute("select test from opens where path=?", (path,))}
        # a directory listing covers files inside it
        d = os.path.dirname(path)
        while d and d != "tests":
            res |= {t for (t,) in self.db.execute("select test from opens where path=?", (d,))}
            d = os.path.dirname(d)
        return res


# ---------------------------------------------------------------- python analysis

def same_tree(a, b):
    return ast.dump(a, include_attributes=False) == ast.dump(b, include_attributes=False)


class _NoneCompare(ast.NodeTransformer):
    """Rewrites `x == None` to `x is None` and `x != None` to `x is not None`.

    Every __eq__/__ne__ in sqlglot (Expression, Dialect and its metaclass)
    returns False against None, as do built-in values, so the two spellings
    behave the same. Comparing normalized trees treats that swap as no change.
    """

    def visit_Compare(self, node):
        self.generic_visit(node)
        operands = [node.left] + node.comparators
        ops = []
        for i, op in enumerate(node.ops):
            a, b = operands[i], operands[i + 1]
            none = any(isinstance(x, ast.Constant) and x.value is None for x in (a, b))
            if none and isinstance(op, ast.Eq):
                op = ast.Is()
            elif none and isinstance(op, ast.NotEq):
                op = ast.IsNot()
            ops.append(op)
        node.ops = ops
        return node


def equivalent_tree(a, b):
    """True when the trees differ only by None-comparison spelling."""
    import copy
    na = _NoneCompare().visit(copy.deepcopy(a))
    nb = _NoneCompare().visit(copy.deepcopy(b))
    return same_tree(na, nb)


def first_line(node):
    return min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])


def header_end(node):
    """Last line of a compound statement's header (before its body)."""
    body = getattr(node, "body", None)
    if isinstance(body, list) and body and isinstance(node, ast.stmt):
        return max(node.lineno, first_line(body[0]) - 1)
    return node.end_lineno


def locate(tree, lineno):
    """Innermost statement containing lineno, and whether it runs inside a function body."""
    best, in_func = None, False

    def walk(node, inside):
        nonlocal best, in_func
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.excepthandler) or (hasattr(ast, "match_case") and isinstance(child, ast.match_case)):
                if child_contains(child, lineno):
                    walk(child, inside)
                continue
            if not isinstance(child, ast.stmt):
                continue
            if first_line(child) <= lineno <= child.end_lineno:
                best, in_func = child, inside
                walk(child, inside or isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)))

    walk(tree, False)
    return best, in_func


def child_contains(node, lineno):
    lo = getattr(node, "lineno", None)
    hi = getattr(node, "end_lineno", None)
    if lo is None:  # match_case has no position; look at its body
        body = getattr(node, "body", [])
        if not body:
            return False
        lo, hi = getattr(node.pattern, "lineno", body[0].lineno), body[-1].end_lineno
    return lo <= lineno <= hi


def lambda_body_at(stmt, lineno):
    """A lambda whose body spans lineno on a line after the lambda keyword, if any."""
    for node in ast.walk(stmt):
        if isinstance(node, ast.Lambda) and node.lineno < lineno <= node.end_lineno:
            return node
    return None


def analyse_python(path, old_text, hunks, cov):
    """Returns (set of tests, reason). None means 'select everything'."""
    new_text, changed = apply_hunks(old_text, hunks)
    try:
        old_tree = ast.parse(old_text)
    except SyntaxError:
        return None, "old file does not parse"
    try:
        new_tree = ast.parse(new_text)
    except SyntaxError:
        return None, "patched file does not parse"
    if same_tree(old_tree, new_tree):
        return set(), "no code change"
    if equivalent_tree(old_tree, new_tree):
        return set(), "equivalent None comparison"
    tests = set()
    module_level = False
    for l in sorted(changed):
        stmt, in_func = locate(old_tree, l)
        if stmt is None:
            module_level = True
            continue
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and l <= header_end(stmt):
            # signature, decorators, defaults or bases: evaluated at definition time
            if in_func:
                tests |= cov.tests_covering(path, range(first_line(stmt), stmt.end_lineno + 1))
            else:
                module_level = True
            continue
        if not in_func:
            lam = lambda_body_at(stmt, l)
            if lam is not None:
                # inside a lambda body: runs only when the lambda is called
                tests |= cov.tests_covering(path, [l])
            else:
                module_level = True
            continue
        lo = first_line(stmt)
        hi = header_end(stmt) if l <= header_end(stmt) else stmt.end_lineno
        tests |= cov.tests_covering(path, range(lo, hi + 1))
    if module_level:
        tests |= cov.tests_touching_file(path)
    return tests, "module level" if module_level else "function body"


def tests_for_test_file(path, old_text, hunks, cov, tests_by_file, nodeid_num):
    new_text, changed = apply_hunks(old_text, hunks)
    whole = set(tests_by_file.get(path, [])) | cov.tests_touching_file(path)
    try:
        old_tree = ast.parse(old_text)
        new_tree = ast.parse(new_text)
    except SyntaxError:
        return whole
    if same_tree(old_tree, new_tree):
        return set()
    res = set()
    for l in changed:
        found = False
        for node in old_tree.body:
            if not (first_line(node) <= l <= node.end_lineno):
                continue
            if isinstance(node, ast.ClassDef):
                for m in node.body:
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and first_line(m) <= l <= m.end_lineno:
                        nid = f"{path}::{node.name}::{m.name}"
                        if nid in nodeid_num:
                            res.add(nodeid_num[nid])
                        else:
                            res |= cov.tests_covering(path, range(first_line(m), m.end_lineno + 1))
                        found = True
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                nid = f"{path}::{node.name}"
                if nid in nodeid_num:
                    res.add(nodeid_num[nid])
                else:
                    res |= cov.tests_covering(path, range(first_line(node), node.end_lineno + 1))
                found = True
        if not found:
            res |= whole
    return res


# ---------------------------------------------------------------- selection

def pack(selected, dur):
    if not selected:
        return []
    items = sorted(selected, key=lambda t: (-dur[t], t))
    best = None
    for w in range(1, MAX_WORKERS + 1):
        loads, bins = [0] * w, [[] for _ in range(w)]
        for t in items:
            k = loads.index(min(loads))
            loads[k] += dur[t]
            bins[k].append(t)
        cost = max(loads) + WORKER_COST_MS * w
        if best is None or cost < best[0]:
            best = (cost, bins)
    return best[1]


# Repository files no test reads (checked: only tests/test_sqlglotc.py reads
# anything outside sqlglot/ and tests/, namely sqlglotc/ and the build config).
NO_TEST_PREFIXES = (".github/", "docs/", "pdoc/", "posts/", "benchmarks/", "sqlglot-integration-tests/")
NO_TEST_FILES = {"README.md", "CHANGELOG.md", "CONTRIBUTING.md", "AGENTS.md", "CLAUDE.md", "LICENSE",
                 ".gitignore", ".gitmodules", ".gitpod.yml", ".pre-commit-config.yaml", "sqlglot.png",
                 "MANIFEST.in", "Makefile"}


def select(text, every, cov, tests_by_file, nodeid_num, reasons):
    selected = set()
    readers_of_typing = set(tests_by_file.get("tests/test_typing.py", []))
    sqlglotc_tests = set(tests_by_file.get("tests/test_sqlglotc.py", []))
    for f in parse_patch(text):
        path = f["old"] or f["new"]
        if f["old"] is None or f["new"] is None or f["old"] != f["new"]:
            reasons.append(f"{path}: added/removed/renamed -> all")
            return set(every)
        full = os.path.join(SRC, path)
        if path.startswith("sqlglot/") and path.endswith(".py"):
            old_text = open(full, encoding="utf-8").read()
            got, why = analyse_python(path, old_text, f["hunks"], cov)
            if got is None:
                reasons.append(f"{path}: {why} -> all")
                return set(every)
            if path.startswith("sqlglot/typing/") and why != "no code change":
                # test_typing reads these files' source and compares annotator
                # code objects without running them; coverage cannot see that.
                got = got | readers_of_typing
            selected |= got
            reasons.append(f"{path}: {why} -> {len(got)}")
        elif path.startswith("tests/") and path.endswith(".py"):
            old_text = open(full, encoding="utf-8").read()
            got = tests_for_test_file(path, old_text, f["hunks"], cov, tests_by_file, nodeid_num)
            selected |= got
            reasons.append(f"{path}: test file -> {len(got)}")
        elif path.startswith("tests/"):
            got = cov.tests_opening(path)
            selected |= got
            reasons.append(f"{path}: data file -> {len(got)}")
        elif path in NO_TEST_FILES or path.startswith(NO_TEST_PREFIXES):
            reasons.append(f"{path}: not read by any test -> 0")
        elif path.startswith("sqlglotc/") or path == "setup.py":
            selected |= sqlglotc_tests
            reasons.append(f"{path}: build script -> test_sqlglotc")
        else:
            reasons.append(f"{path}: unknown kind -> all")
            return set(every)
    return selected


def main():
    tests = json.load(open(os.path.join(DATA, "tests.json")))["tests"]
    dur = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    every = set(dur)
    nodeid_num = {t["id"]: t["number"] for t in tests}
    tests_by_file = {}
    for t in tests:
        tests_by_file.setdefault(t["file"], []).append(t["number"])
    cov = Map(MAP)
    cdir = os.path.join(DATA, "commits")
    for name in sorted(os.listdir(cdir)):
        if not name.endswith(".patch"):
            continue
        cid = name[:-len(".patch")]
        reasons = []
        try:
            text = open(os.path.join(cdir, name), encoding="utf-8", errors="replace").read()
            selected = select(text, every, cov, tests_by_file, nodeid_num, reasons) & every
        except Exception as e:  # never crash: a crash rejects the run; a mismatch must not miss
            selected = set(every)
            reasons.append(f"{type(e).__name__} {e} -> all")
        print(json.dumps({"commit": cid, "workers": pack(selected, dur)}, separators=(",", ":")))
        log(cid, "; ".join(reasons))


if __name__ == "__main__":
    main()
