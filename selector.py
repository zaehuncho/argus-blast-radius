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
CLASSMAP = os.environ.get("ARGUS_CLASSMAP", os.path.join(os.path.dirname(MAP), "classes.sqlite"))
WORKER_COST_MS = 2000
MAX_WORKERS = 8

# Class-usage map (classmap.py): which tests read each class attribute, entered
# each lambda, or used each class. Without it, class-level edits stay broad.
CM = None
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import classmap
    if os.path.exists(CLASSMAP):
        CM = classmap.ClassMap(CLASSMAP)
except Exception as _e:  # selection stays correct, only broader
    print("classmap unavailable:", repr(_e), file=sys.stderr)


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
        run_deleted = False  # the current run of edits removed old lines
        for tag, text in body:
            if tag == " ":
                if o >= len(old) or old[o].rstrip("\r") != text.rstrip("\r"):
                    raise PatchMismatch(f"context differs at line {o + 1}")
                out.append(old[o])
                o += 1
                run_deleted = False
            elif tag == "-":
                if o >= len(old) or old[o].rstrip("\r") != text.rstrip("\r"):
                    raise PatchMismatch(f"removed line differs at line {o + 1}")
                changed.add(o + 1)
                o += 1
                edits += 1
                run_deleted = True
            elif tag == "+":
                out.append(text)
                if not run_deleted:
                    # a pure insertion sits between old lines o and o+1 (1-based)
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


# ---------------------------------------------------------------- TPC-DS check
#
# test_execute_tpcds (21.5 s recorded) parses, optimizes, plans, executes and
# compares 57 TPC-DS queries. When line coverage alone selects it and none of the
# changed statements runs anywhere in the test except the parse/optimize step
# (map/tpcds_block.json, tools/build_tpcds_block.sh), tpcds_check.py decides it for
# about 2.5 s of CPU: identical optimized trees mean identical results.

TPCDS_ID = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
TPCDS_BLOCK = os.environ.get("ARGUS_TPCDS_BLOCK", os.path.join(os.path.dirname(MAP), "tpcds_block.json"))
TPCDS_PRINTS = os.environ.get("ARGUS_TPCDS_PRINTS", os.path.join(os.path.dirname(MAP), "tpcds_prints.json"))
TPCDS_CHECK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tpcds_check.py")
TPCDS_CHECK_CPU_S = 2.6   # measured 2.51 s in the production image
TPCDS_P_DROP = 0.5        # share of checks expected to come back identical
# When only a few queries' trees differ, the test narrowed to them (runner.py's
# ARGUS_TPCDS_ONLY) decides it: every other query provably behaves as before.
TPCDS_MAX_DIFF = 6
TPCDS_RUN_FIXED_S, TPCDS_RUN_PAIR_S = 1.6, 1.0   # narrowed run CPU (measured 1.55 s + ~0.8 s a query)
TPCDS_P_PASS_FEW = 0.75   # validation: 15 of 19 cases with 1-6 differing queries passed


# The fixture holding the queries: test_execute_tpcds uses each pair's input SQL and
# "execute" flag only, which the check re-reads from the patched file.
TPCDS_FIXTURE = "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql"


class TpcdsBlock:
    def __init__(self, path):
        with open(path, encoding="utf-8") as fh:
            self.lines = {f: set(v) for f, v in json.load(fh)["lines"].items()}

    def clear(self, queries, fixture=False):
        """True when no queried line runs outside the parse/optimize step."""
        if not queries and not fixture:
            return False
        for path, linenos in queries:
            block = self.lines.get(path, ())
            if any(l in block for l in linenos):
                return False
        return True


TP = None
try:
    if os.path.exists(TPCDS_BLOCK) and os.path.exists(TPCDS_PRINTS) and os.path.exists(TPCDS_CHECK):
        TP = TpcdsBlock(TPCDS_BLOCK)
except Exception as _e:  # no check, selection unchanged
    print("tpcds block map unavailable:", repr(_e), file=sys.stderr)


class LineLog:
    """The coverage map for one selection pass. Records the line queries whose answer
    includes `test`; with drop=True hides `test` from line queries instead, to see
    whether anything other than line coverage selects it."""

    def __init__(self, cov, test, drop=False):
        self._cov, self._test, self._drop = cov, test, drop
        self.queries = []
        self.fixture = False

    def tests_covering(self, path, linenos):
        linenos = list(linenos)
        res = self._cov.tests_covering(path, linenos)
        if self._test in res:
            if self._drop:
                return res - {self._test}
            self.queries.append((path, linenos))
        return res

    def tests_opening(self, path):
        res = self._cov.tests_opening(path)
        if self._test in res and path == TPCDS_FIXTURE:
            if self._drop:
                return res - {self._test}
            self.fixture = True
        return res

    def __getattr__(self, name):
        return getattr(self._cov, name)


# ---------------------------------------------------------------- python analysis

def same_tree(a, b):
    return ast.dump(a, include_attributes=False) == ast.dump(b, include_attributes=False)


class _NoneCompare(ast.NodeTransformer):
    """Rewrites `x == None` to `x is None` and `x != None` to `x is not None`.

    Every __eq__/__ne__ in sqlglot (Expression, Dialect and its metaclass)
    returns False against None, as do built-in values, so the two spellings
    behave the same. Comparing normalized trees treats that swap as no change.
    """

    # Enum members where `==` behaves like `is`. Plain enums compare by identity.
    # NormalizationStrategy, UsingColumnOrder and AliasPrecedence mix in str, but
    # every value they are compared with in sqlglot is a member: class defaults are
    # members and string settings go through the enum constructor. Dialects (str,
    # equal to dialect classes by name) and TokenType (IntEnum) are left out.
    _ENUMS = {"ScopeType", "IsPredicatePrecedence", "TrieResult", "ErrorLevel", "DType", "PropertiesLocation",
              "NormalizationStrategy", "UsingColumnOrder", "AliasPrecedence"}
    _NESTED_ENUMS = {("DataType", "Type"), ("Properties", "Location")}

    @classmethod
    def _enum_member(cls, x):
        if not (isinstance(x, ast.Attribute) and x.attr.isupper()):
            return False
        owner = x.value
        if isinstance(owner, ast.Name):
            return owner.id in cls._ENUMS
        if isinstance(owner, ast.Attribute):
            if owner.attr in cls._ENUMS:
                return True
            return isinstance(owner.value, (ast.Attribute, ast.Name)) and (
                (owner.value.attr if isinstance(owner.value, ast.Attribute) else owner.value.id), owner.attr) in cls._NESTED_ENUMS
        return False

    @classmethod
    def _identity_only(cls, x):
        # None, or an expression class (`exp.Foo`): classes compare by identity
        # unless their metaclass defines __eq__, and sqlglot's only one (_Dialect)
        # returns False for anything that is not a dialect or a string.
        if isinstance(x, ast.Constant) and x.value is None:
            return True
        if cls._enum_member(x):
            return True
        return (isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "exp"
                and x.attr[:1].isupper() and not x.attr.isupper()
                and x.attr not in ("ExpOrStr", "IntoType"))  # typing aliases, not classes

    def visit_Compare(self, node):
        self.generic_visit(node)
        operands = [node.left] + node.comparators
        ops = []
        for i, op in enumerate(node.ops):
            a, b = operands[i], operands[i + 1]
            none = any(self._identity_only(x) for x in (a, b))
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


def _text(lines, node):
    return "\n".join(lines[first_line(node) - 1:node.end_lineno])


def _dump(node, norm):
    if norm:
        import copy
        node = _NoneCompare().visit(copy.deepcopy(node))
    return ast.dump(node, include_attributes=False)


def _is_docstring(node):
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)


def _same_body(xs, ys, la, lb, norm):
    """Statement lists compare equal. Identical source text means an identical
    tree, so only statements whose text differs are dumped, recursing into
    classes and functions so a one-line edit never dumps a whole class.

    With norm, docstring text is ignored too: nothing in sqlglot reads __doc__
    contents (core.py only defaults a missing one to ""), and the doctest
    module is not collected by pytest."""
    if len(xs) != len(ys):
        return False
    for i, (x, y) in enumerate(zip(xs, ys)):
        if type(x) is not type(y):
            return False
        if _text(la, x) == _text(lb, y):
            continue
        if norm and i == 0 and _is_docstring(x) and _is_docstring(y):
            continue
        if isinstance(x, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            bx, by = x.body, y.body
            try:
                x.body, y.body = [], []
                same_header = _dump(x, norm) == _dump(y, norm)
            finally:
                x.body, y.body = bx, by
            if not same_header or not _same_body(bx, by, la, lb, norm):
                return False
            continue
        if _dump(x, norm) != _dump(y, norm):
            return False
    return True


def trees_match(old_tree, new_tree, old_text, new_text, norm=False):
    """Fast equivalent of same_tree (norm=False) or equivalent_tree (norm=True)."""
    return _same_body(old_tree.body, new_tree.body, old_text.split("\n"), new_text.split("\n"), norm)


# Standard-library modules whose import has no visible side effect.
_STDLIB = set(getattr(sys, "stdlib_module_names", ())) - {
    "__future__", "this", "antigravity", "turtle", "turtledemo", "idlelib", "tkinter", "webbrowser"}


def _stdlib_import(node):
    if isinstance(node, ast.Import):
        return all(a.name.split(".")[0] in _STDLIB for a in node.names)
    if isinstance(node, ast.ImportFrom):
        return (node.level == 0 and bool(node.module) and node.module.split(".")[0] in _STDLIB
                and not any(a.name == "*" for a in node.names))
    return False


def _bound(node):
    return {a.asname or a.name.split(".")[0] for a in node.names}


def _names(tree):
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
        elif isinstance(n, (ast.Import, ast.ImportFrom)) and not _stdlib_import(n):
            out |= _bound(n)
        elif isinstance(n, ast.arg):
            out.add(n.arg)
    return out


def stdlib_imports_only(old_tree, new_tree, old_text, new_text):
    """True when the modules differ only in top-level standard-library imports
    that are reordered without crossing other statements, added under names the
    old module never used, or removed under names the new module never uses."""
    def split(tree):
        imports, rest, slot = [], [], 0
        for n in tree.body:
            if _stdlib_import(n):
                imports.append((slot, ast.dump(n, include_attributes=False), n))
            else:
                rest.append(n)
                slot += 1
        return imports, rest
    oi, orest = split(old_tree)
    ni, nrest = split(new_tree)
    if not oi and not ni:
        return False
    if not _same_body(orest, nrest, old_text.split("\n"), new_text.split("\n"), False):
        return False
    old_set = {(s, d) for s, d, _ in oi}
    new_set = {(s, d) for s, d, _ in ni}
    old_dumps = {d for _, d, _ in oi}
    new_dumps = {d for _, d, _ in ni}
    old_names, new_names = _names(old_tree), _names(new_tree)
    for s, d, n in ni:
        if (s, d) in old_set:
            continue
        if d in old_dumps:
            return False  # an existing import moved past other statements
        if _bound(n) & old_names:
            return False  # a new import that binds a name the module already used
    for s, d, n in oi:
        if d not in new_dumps and _bound(n) & new_names:
            return False  # a removed import whose name is still used
    return True


def _inert(node):
    """An expression that cannot run code or raise when evaluated at def time."""
    if node is None or isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd, ast.Not)):
        return _inert(node.operand)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(_inert(e) for e in node.elts)
    if isinstance(node, ast.Dict):
        return all(_inert(k) and _inert(v) for k, v in zip(node.keys, node.values))
    return False


def signature_defaults_only(old_def, new_tree, old_tree):
    """The def differs only in default values (inert on both sides) or in
    annotations. Defaults are evaluated once at def time and used only by calls
    that omit the argument, and those calls run the function body."""
    if not isinstance(old_def, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    path = _qualpath(old_tree, old_def)
    new_def = _find_def(new_tree, path) if path else None
    if new_def is None or type(new_def) is not type(old_def):
        return False
    oa, na = old_def.args, new_def.args
    names = lambda a: [x.arg for x in a.posonlyargs + a.args + a.kwonlyargs]  # noqa: E731
    if (names(oa) != names(na) or len(oa.defaults) != len(na.defaults) or len(oa.kw_defaults) != len(na.kw_defaults)
            or bool(oa.vararg) != bool(na.vararg) or bool(oa.kwarg) != bool(na.kwarg)):
        return False
    for o, n in zip(oa.defaults + oa.kw_defaults, na.defaults + na.kw_defaults):
        same = (ast.dump(o) if o is not None else None) == (ast.dump(n) if n is not None else None)
        if not same and not (_inert(o) and _inert(n)):
            return False
    saved = (old_def.args, new_def.args, old_def.returns, new_def.returns)
    try:
        old_def.args = new_def.args = None
        old_def.returns = new_def.returns = None
        return ast.dump(old_def, include_attributes=False) == ast.dump(new_def, include_attributes=False)
    finally:
        old_def.args, new_def.args, old_def.returns, new_def.returns = saved


_DESCRIPTORS = {"classmethod", "staticmethod", "property"}


def _qualpath(tree, target):
    """Names of the classes/functions enclosing target, outermost first."""
    def walk(node, path):
        for child in ast.iter_child_nodes(node):
            if child is target:
                return path + [child.name]
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                got = walk(child, path + [child.name])
                if got:
                    return got
        return None
    return walk(tree, [])


def _find_def(tree, path):
    node = tree
    for name in path:
        nxt = [c for c in getattr(node, "body", []) if isinstance(c, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and c.name == name]
        if len(nxt) != 1:
            return None
        node = nxt[0]
    return node


def descriptor_swap_only(old_def, new_tree, old_tree):
    """The def differs only by adding, removing or swapping classmethod,
    staticmethod or property decorators. Such a change takes effect only when
    the function is called or the property read, which runs its body."""
    if not isinstance(old_def, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    path = _qualpath(old_tree, old_def)
    new_def = _find_def(new_tree, path) if path else None
    if new_def is None or type(new_def) is not type(old_def):
        return False

    def plain(d):
        return [x for x in d.decorator_list if not (isinstance(x, ast.Name) and x.id in _DESCRIPTORS)]
    if [ast.dump(x) for x in plain(old_def)] != [ast.dump(x) for x in plain(new_def)]:
        return False
    ob, nb = old_def.decorator_list, new_def.decorator_list
    try:
        old_def.decorator_list, new_def.decorator_list = [], []
        return ast.dump(old_def, include_attributes=False) == ast.dump(new_def, include_attributes=False)
    finally:
        old_def.decorator_list, new_def.decorator_list = ob, nb


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


# ---------------------------------------------------------------- module constants
#
# A top-level `NAME = value` edit changes what NAME is bound to. When every mention of
# NAME anywhere in sqlglot/ and tests/ sits inside a function or lambda body (it is
# looked up when that code runs, never while modules or classes are being built), the
# tests are those whose coverage runs one of those statements. A mention evaluated at
# import (a module-level statement, a class body, a default, a decorator) keeps the
# broad rule. Values that are not inert must call the same functions as before, and the
# module must still import (checked in the container).

_SOURCES = None
_TREES = {}


def _sources():
    global _SOURCES
    if _SOURCES is None:
        _SOURCES = {}
        for top in ("sqlglot", "tests"):
            for dirpath, dirnames, filenames in os.walk(os.path.join(SRC, top)):
                dirnames[:] = [d for d in dirnames if d != "__pycache__"]
                for fn in filenames:
                    if fn.endswith(".py"):
                        full = os.path.join(dirpath, fn)
                        rel = os.path.relpath(full, SRC).replace(os.sep, "/")
                        with open(full, encoding="utf-8") as fh:
                            _SOURCES[rel] = fh.read()
    return _SOURCES


def _tree(rel):
    if rel not in _TREES:
        _TREES[rel] = ast.parse(_sources()[rel])
    return _TREES[rel]


def _mention_sites(name, defining_path, defining_stmt_lines, removed=False):
    """[(path, first line, last line)] of the statements inside function bodies that
    mention `name` (as a name, attribute or string), and [(path, line)] of the lambdas
    built at import whose bodies mention it; None if any mention is evaluated at import
    time, a docstring mentions it (doctests), or it is imported under another name."""
    word = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])")
    sites, lams = [], []
    for rel, text in _sources().items():
        if name not in text or not word.search(text):
            continue
        tree = _tree(rel)

        def visit(node, stmt, in_body, in_all, lam=None):
            hit = False
            if isinstance(node, ast.Name) and node.id == name:
                hit = not isinstance(node.ctx, ast.Store) or in_body
            elif isinstance(node, ast.Attribute) and node.attr == name:
                hit = True
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and word.search(node.value):
                if in_all and removed:
                    return False  # a star import of a listed but removed name fails
                if not in_all:
                    if not in_body or node.value != name:
                        return False  # docstrings and other text: doctests could read it
                    hit = True
            elif isinstance(node, ast.alias) and (node.name == name or node.name.endswith("." + name)):
                if node.asname and node.asname != name:
                    return False  # uses under the other name are not found by this search
                if in_body:
                    hit = True
                elif removed:
                    return False  # a top-level `from m import NAME` of a removed name fails at import
                # a top-level `from m import NAME` only binds it; its uses are found as names
            if hit:
                if not in_body:
                    if rel == defining_path and stmt is not None and stmt.lineno in defining_stmt_lines:
                        pass  # the defining statement itself
                    else:
                        return False
                elif lam is not None:
                    lams.append((rel, lam.lineno))  # a lambda built at import: who called it
                else:
                    sites.append((rel, stmt.lineno, stmt.end_lineno))
            for field, value in ast.iter_fields(node):
                children = value if isinstance(value, list) else [value]
                for child in children:
                    if not isinstance(child, ast.AST):
                        continue
                    child_stmt = child if isinstance(child, ast.stmt) else stmt
                    child_body, child_lam = in_body, lam
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and field == "body":
                        child_body, child_lam = True, None
                    elif isinstance(node, ast.Lambda) and field == "body":
                        if not in_body:
                            child_lam = node
                        child_body = True
                    child_all = in_all or (
                        isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)) and not in_body
                        and any(isinstance(t, ast.Name) and t.id == "__all__"
                                for t in (node.targets if isinstance(node, ast.Assign) else [node.target]))
                    )
                    if visit(child, child_stmt, child_body, child_all, child_lam) is False:
                        return False
            return None

        if visit(tree, None, False, False) is False:
            return None
    return sites, lams


def _binds(st, proven=None, lazy_annotations=False):
    """Names a top-level statement that may be added or removed binds: `NAME = value`
    (one plain name), a standard-library import (importing one cannot register
    anything in sqlglot), or an undecorated def whose defaults are inert and whose
    annotations are not evaluated (`from __future__ import annotations`) or absent;
    None for anything else."""
    if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
        a = st.args
        if st.decorator_list or getattr(st, "type_params", None) or proven is None:
            return None
        defaults = a.defaults + [d for d in a.kw_defaults if d is not None]
        if not all(classmap._inert(d, proven) for d in defaults):
            return None
        params = a.posonlyargs + a.args + a.kwonlyargs + [x for x in (a.vararg, a.kwarg) if x is not None]
        if (st.returns is not None or any(p.annotation is not None for p in params)) and not lazy_annotations:
            return None
        return {st.name}
    if isinstance(st, (ast.Assign, ast.AnnAssign)):
        targets = st.targets if isinstance(st, ast.Assign) else [st.target]
        if len(targets) == 1 and isinstance(targets[0], ast.Name) and st.value is not None:
            return {targets[0].id}
        return None
    if isinstance(st, ast.Import):
        if all(a.name.split(".")[0] in _STDLIB for a in st.names):
            return {a.asname or a.name.split(".")[0] for a in st.names}
        return None
    if isinstance(st, ast.ImportFrom):
        if st.level == 0 and st.module and st.module.split(".")[0] in _STDLIB and all(a.name != "*" for a in st.names):
            return {a.asname or a.name for a in st.names}
    return None


def module_constants(path, old_tree, new_tree, cov):
    """(tests, needs import check, names) for a file whose top-level changes are all
    `NAME = value` edits, or added/removed `NAME = value` statements and standard-library
    imports, that the rule above can bound (def bodies are left to line coverage);
    None otherwise."""
    if CM is None:
        return None
    import difflib
    proven = classmap._Proven(old_tree)
    names, needs_import, lines, removed = set(), False, {}, set()

    def lazy(tree):
        return any(isinstance(s, ast.ImportFrom) and s.module == "__future__"
                   and any(a.name == "annotations" for a in s.names) for s in tree.body)

    lazy_old, lazy_new = lazy(old_tree), lazy(new_tree)
    if lazy_old != lazy_new:
        return None
    da, db = [ast.dump(s) for s in old_tree.body], [ast.dump(s) for s in new_tree.body]
    pairs = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=da, b=db, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if tag == "replace" and i2 - i1 == j2 - j1:
            pairs.extend(zip(old_tree.body[i1:i2], new_tree.body[j1:j2]))
            continue
        for st in old_tree.body[i1:i2]:  # removed: later lookups of what it bound change
            got = _binds(st, proven, lazy_old)
            if got is None:
                return None
            for n in got:
                if n.startswith("__"):
                    return None
                names.add(n)
                removed.add(n)
                lines.setdefault(n, set()).update(range(st.lineno, st.end_lineno + 1))
        for st in new_tree.body[j1:j2]:  # added: it runs at import
            got = _binds(st, proven, lazy_new)
            if got is None:
                return None
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                pass  # only binds its name (checked above): its body runs when called
            elif isinstance(st, (ast.Assign, ast.AnnAssign)):
                if not classmap._inert(st.value, proven):
                    if not classmap._same_callees(st.value, st.value):
                        return None
                    needs_import = True
            else:
                needs_import = True  # the imported names must exist
            for n in got:
                if n.startswith("__"):
                    return None
                names.add(n)
    for a, b in pairs:
        if ast.dump(a) == ast.dump(b):
            continue
        if isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef)) and type(a) is type(b):
            if classmap._def_header(a) == classmap._def_header(b):
                continue  # body edits: line coverage
            # a new header (renamed, new parameters or defaults): every caller of either name
            ga, gb = _binds(a, proven, lazy_old), _binds(b, proven, lazy_new)
            if ga is None or gb is None:
                return None
            for n in ga | gb:
                if n.startswith("__"):
                    return None
                names.add(n)
            if a.name != b.name:
                removed.add(a.name)
            lines.setdefault(a.name, set()).update(range(a.lineno, a.end_lineno + 1))
            continue
        if not (isinstance(a, (ast.Assign, ast.AnnAssign)) and type(a) is type(b)):
            return None
        ta = a.targets if isinstance(a, ast.Assign) else [a.target]
        tb = b.targets if isinstance(b, ast.Assign) else [b.target]
        if len(ta) != 1 or len(tb) != 1 or not isinstance(ta[0], ast.Name) or ast.dump(ta[0]) != ast.dump(tb[0]):
            return None
        if isinstance(a, ast.AnnAssign) and (a.value is None or b.value is None
                                             or ast.dump(a.annotation) != ast.dump(b.annotation)):
            return None
        name = ta[0].id
        if name.startswith("__"):
            return None
        if not classmap._safe(a.value, b.value, proven):
            if not classmap._same_callees(a.value, b.value):
                return None
            needs_import = True
        names.add(name)
        lines.setdefault(name, set()).update(range(a.lineno, a.end_lineno + 1))
    if not names:
        return None
    tests = set()
    for name in names:
        found = _mention_sites(name, path, lines.get(name, set()), removed=name in removed)
        if found is None:
            return None
        sites, lams = found
        for rel, lo, hi in sites:
            tests |= cov.tests_covering(rel, range(lo, hi + 1))
        for rel, line in lams:
            tests |= CM.entered(rel, line)
    return tests, needs_import, names


def analyse_python(path, old_text, hunks, cov, checks=None):
    """Returns (set of tests, reason). None means 'select everything'. A narrowing that
    holds only if the patched module still imports appends `path` to `checks`
    (without `checks` such a narrowing is not used)."""
    new_text, changed = apply_hunks(old_text, hunks)
    try:
        old_tree = ast.parse(old_text)
    except SyntaxError:
        return None, "old file does not parse"
    try:
        new_tree = ast.parse(new_text)
    except SyntaxError:
        return None, "patched file does not parse"
    if trees_match(old_tree, new_tree, old_text, new_text):
        return set(), "no code change"
    if trees_match(old_tree, new_tree, old_text, new_text, norm=True):
        return set(), "equivalent (None comparison / docstring)"
    if stdlib_imports_only(old_tree, new_tree, old_text, new_text):
        return set(), "standard-library imports only"
    tests = set()
    module_level = False
    for l in sorted(changed):
        stmt, in_func = locate(old_tree, l)
        if stmt is None:
            module_level = True
            continue
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and l <= header_end(stmt):
            # signature, decorators, defaults or bases: evaluated at definition time
            if l < stmt.lineno and descriptor_swap_only(stmt, new_tree, old_tree):
                # a decorator line: classmethod/staticmethod/property changes only
                # matter where the function is called or the property read, which
                # runs its body (the def and decorator lines run at class creation)
                tests |= cov.tests_covering(path, range(first_line(stmt.body[0]), stmt.end_lineno + 1))
                continue
            if l >= stmt.lineno and signature_defaults_only(stmt, new_tree, old_tree):
                # a default value or annotation: only calls that run the body see it
                tests |= cov.tests_covering(path, range(first_line(stmt.body[0]), stmt.end_lineno + 1))
                continue
            if in_func:
                tests |= cov.tests_covering(path, range(first_line(stmt), stmt.end_lineno + 1))
            else:
                module_level = True
            continue
        if not in_func:
            lam = lambda_body_at(stmt, l)
            if lam is not None and CM is not None:
                # inside a lambda body: runs only when the lambda is called. A changed
                # line may be one that short-circuiting skipped, so take every test
                # that entered the lambda.
                tests |= cov.tests_covering(path, [l]) | CM.entered(path, lam.lineno)
            else:
                module_level = True
            continue
        lo = first_line(stmt)
        hi = header_end(stmt) if l <= header_end(stmt) else stmt.end_lineno
        tests |= cov.tests_covering(path, range(lo, hi + 1))
    if module_level:
        mc = None
        try:
            mc = module_constants(path, old_tree, new_tree, cov)
        except Exception as e:  # the broad rule stands
            log("module constant rule error", path, repr(e))
        if mc is not None and mc[1]:
            if checks is None:
                mc = None
            else:
                checks.append(path)
        if mc is not None:
            return tests | mc[0], "module constants " + ",".join(sorted(mc[2])) + (" (if it imports)" if mc[1] else "")
        narrowed = None
        if CM is not None:
            try:
                narrowed = classmap.narrow(CM, cov, path, old_text, new_text, old_tree, new_tree)
            except Exception as e:  # the broad rule stands
                log("classmap error", path, repr(e))
        if narrowed is not None and narrowed[2]:
            if checks is None:
                narrowed = None
            else:
                checks.append(path)
        if narrowed is not None:
            return tests | narrowed[0], "class map: " + narrowed[1]
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
    if trees_match(old_tree, new_tree, old_text, new_text):
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


def file_op_tests(f, cov, tests_by_file, every, readers_of_typing, sqlglotc_tests):
    """Tests for an added, deleted or renamed file; None means select everything."""
    old, new = f["old"], f["new"]
    res = set()
    for p, gone in ((old, True), (new, False)):
        if p is None or (p == old and p == new):
            continue
        if p in NO_TEST_FILES or p.startswith(NO_TEST_PREFIXES):
            continue
        if p.startswith("tests/") and not p.endswith(".py"):
            # data files: whoever read it (gone) or lists its directory (both)
            res |= cov.tests_opening(p)
            continue
        if p.startswith("sqlglot/") and p.endswith(".py"):
            if gone:
                # every test whose process loaded the module now fails to import it
                res |= cov.tests_touching_file(p) or set(every)
            # sqlglotc's build lists package files; test_typing scans sqlglot/typing
            res |= sqlglotc_tests
            if p.startswith("sqlglot/typing/"):
                res |= readers_of_typing
            continue
        if p.startswith("tests/") and p.endswith(".py"):
            if not gone:
                return None  # a new test module could do anything at collection
            res |= set(tests_by_file.get(p, [])) | cov.tests_touching_file(p)
            continue
        if p.startswith("sqlglotc/") or p == "setup.py":
            res |= sqlglotc_tests
            continue
        return None
    # added Python modules are reached only through an edit elsewhere in the patch,
    # which its own file's rule covers; renamed/modified content is handled above
    if old and new and old != new and f["hunks"] and new.endswith(".py"):
        return None
    return res


# Repository files no test reads (checked: only tests/test_sqlglotc.py reads
# anything outside sqlglot/ and tests/, namely sqlglotc/ and the build config).
NO_TEST_PREFIXES = (".github/", "docs/", "pdoc/", "posts/", "benchmarks/", "sqlglot-integration-tests/")
NO_TEST_FILES = {"README.md", "CHANGELOG.md", "CONTRIBUTING.md", "AGENTS.md", "CLAUDE.md", "LICENSE",
                 ".gitignore", ".gitmodules", ".gitpod.yml", ".pre-commit-config.yaml", "sqlglot.png",
                 "MANIFEST.in", "Makefile"}


def select(text, every, cov, tests_by_file, nodeid_num, reasons, checks=None):
    selected = set()
    readers_of_typing = set(tests_by_file.get("tests/test_typing.py", []))
    sqlglotc_tests = set(tests_by_file.get("tests/test_sqlglotc.py", []))
    for f in parse_patch(text):
        path = f["old"] or f["new"]
        if f["old"] is None or f["new"] is None or f["old"] != f["new"]:
            got = file_op_tests(f, cov, tests_by_file, every, readers_of_typing, sqlglotc_tests)
            if got is None:
                reasons.append(f"{path}: added/removed/renamed -> all")
                return set(every)
            selected |= got
            reasons.append(f"{f['old']} -> {f['new']}: file op -> {len(got)}")
            continue
        full = os.path.join(SRC, path)
        if path in ("setup.cfg", "pyproject.toml", "tox.ini", "pytest.ini"):
            old_text = open(full, encoding="utf-8").read() if os.path.exists(full) else ""
            new_text, _ = apply_hunks(old_text, f["hunks"])
            if "pytest" in old_text.lower() or "pytest" in new_text.lower() or path in ("tox.ini", "pytest.ini"):
                reasons.append(f"{path}: may configure pytest -> all")
                return set(every)
            # mypy/build settings only; setuptools_scm in test_sqlglotc reads pyproject.toml
            got = sqlglotc_tests if path == "pyproject.toml" else set()
            selected |= got
            reasons.append(f"{path}: no pytest settings -> {len(got)}")
            continue
        if path.startswith("sqlglot/") and path.endswith(".py"):
            old_text = open(full, encoding="utf-8").read()
            got, why = analyse_python(path, old_text, f["hunks"], cov, checks)
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


def cost_ms(workers, dur):
    workers = [w for w in workers if w]
    if not workers:
        return 0
    return max(sum(dur[t] for t in w) for w in workers) + WORKER_COST_MS * len(workers)


# Runner CPU model from runner.py's measurements (fixed s, s per recorded s), scaled
# by 1.5: the scorer's machine used ~22 s of runner CPU on a batch where ours
# predicted roughly two thirds of that.
RUN_SCALE = float(os.environ.get("ARGUS_RUN_SCALE", "1.5"))
RUN_MODEL = {k: (f * RUN_SCALE, p * RUN_SCALE) for k, (f, p) in
             {"plain": (0.45, 1.0), "optimizer": (1.25, 1.4), "executor": (2.2, 2.0)}.items()}
RUN_MARGIN_S = float(os.environ.get("ARGUS_RUN_MARGIN", "1.0"))          # run only if predicted CPU is this much below the static cost
RUN_BUDGET_PER_COMMIT_S = 2.0
RUN_TIMEOUT_S = 25.0


def runner_kind(selected, tfile):
    files = {tfile[t] for t in selected}
    if "tests/test_executor.py" in files:
        return "executor"
    if "tests/test_optimizer.py" in files:
        return "optimizer"
    return "plain"


def confirm_by_running(plans, dur, tfile, tid, nodeid_num, every):
    """Runs the cheapest-to-confirm candidate sets and keeps only the tests that fail."""
    if os.environ.get("ARGUS_NO_RUNNER") or not os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)), "runner.py")):
        return
    import time
    try:
        import runner
    except Exception as e:
        log("runner unavailable:", e)
        return
    budget = RUN_BUDGET_PER_COMMIT_S * len(plans)
    start = time.monotonic()
    tpcds_checks(plans, dur, runner, budget, start)
    order = []
    for p in plans:
        sel = p["selected"]
        if not sel or sel == every:
            continue
        static = cost_ms(pack(sel, dur), dur) / 1000
        fixed, per = RUN_MODEL[runner_kind(sel, tfile)]
        est = fixed + per * sum(dur[t] for t in sel) / 1000
        if static - est >= RUN_MARGIN_S:
            order.append((static - est, est, p))
    order.sort(key=lambda x: -x[0])
    # A warm fork server answers like a fresh process (runner.py's checks). Preloading
    # numpy/pandas/duckdb cost more than it saved on the scorer (v6.2: 7.0 s CPU against
    # v6.1's 5.3 s on identical selections), so the server preloads pytest only: locally
    # 0.45 s less CPU on the samples and 2.0 s less on 91 sample-style commits.
    mode = "fresh"
    preload = os.environ.get("ARGUS_FORK_PRELOAD", "")
    if len(order) >= 4 and not os.environ.get("ARGUS_NO_FORK"):
        try:
            runner.start_fork_servers(preload=preload)
            mode = "fork"
        except Exception as e:
            log("fork server unavailable:", repr(e))
    try:
        for gain, est, p in order:
            left = budget - (time.monotonic() - start)
            if left < est * 1.5 + 1:
                continue
            try:
                files = runner.files_from_patch(p["text"])
                res = runner.run_candidates(files, sorted(tid[t] for t in p["selected"]),
                                            timeout_s=min(RUN_TIMEOUT_S, left), mode=mode, preload=preload)
            except Exception as e:
                p["reasons"].append(f"runner error {type(e).__name__} -> static")
                continue
            _use_result(p, res, nodeid_num)
    finally:
        if mode == "fork":
            try:
                runner.stop_fork_servers()
            except Exception:
                pass


IMPORT_CODE = "import importlib, sys\nfor m in sys.argv[1:]:\n    importlib.import_module(m)\n"


def import_checks(plans, every):
    """Class-map narrowings that hold only if the patched modules still import: import
    them on the patched tree; anything but a clean import selects every test."""
    pending = [p for p in plans if p.get("imports")]
    if not pending:
        return
    runner = None
    if not os.environ.get("ARGUS_NO_RUNNER"):
        try:
            import runner
        except Exception as e:
            log("runner unavailable for import checks:", repr(e))
    for p in pending:
        mods = sorted({classmap.module_of(path) for path in p["imports"]})
        res = None
        if runner is not None:
            try:
                files = runner.files_from_patch(p["text"])
                res = runner.run_script(files, ["-c", IMPORT_CODE, *mods], timeout_s=20.0)
            except Exception as e:
                log(p["cid"], "import check error", repr(e))
        if res is not None and res.get("status") == "ok" and res.get("exitcode") == 0:
            p["reasons"].append(f"import check ok in {res['cpu_s']:.2f}s cpu")
        else:
            p["selected"] = set(every)
            p["tpcds_check"] = False
            p["reasons"].append("import check " + (f"{res.get('status')} exit={res.get('exitcode')}" if res else "not run")
                                + " -> all")


def tpcds_checks(plans, dur, runner, budget, start):
    """Runs tpcds_check.py for the commits where it applies and pays off; drops
    test_execute_tpcds where every optimized tree comes out as on the pinned commit."""
    import time
    order = []
    for p in plans:
        tp, sel = p.get("tpcds_test"), p["selected"]
        if not p.get("tpcds_check") or tp not in sel:
            continue
        static = cost_ms(pack(sel, dur), dur) / 1000
        without = cost_ms(pack(sel - {tp}, dur), dur) / 1000
        est = RUN_SCALE * TPCDS_CHECK_CPU_S
        gain = (static - without) * TPCDS_P_DROP - est
        if gain >= RUN_MARGIN_S:
            order.append((gain, est, p))
    order.sort(key=lambda x: -x[0])
    for gain, est, p in order:
        left = budget - (time.monotonic() - start)
        if left < est * 1.5 + 1:
            continue
        tp = p["tpcds_test"]
        try:
            files = runner.files_from_patch(p["text"])
            res = runner.run_script(files, [TPCDS_CHECK, "--check", TPCDS_PRINTS, str(TPCDS_MAX_DIFF)],
                                    timeout_s=min(20.0, left))
        except Exception as e:
            p["reasons"].append(f"tpcds check error {type(e).__name__} -> kept")
            continue
        ok = res.get("status") == "ok"
        if ok and res.get("exitcode") == 0:
            p["selected"] = p["selected"] - {tp}
            p["reasons"].append(f"tpcds check identical in {res['cpu_s']:.2f}s cpu -> dropped")
            continue
        m = re.search(r"TPCDS_DIFFER ([0-9,]+)", res.get("log_tail", "")) if ok and res.get("exitcode") == 4 else None
        p["reasons"].append(f"tpcds check {res.get('status')} exit={res.get('exitcode')} "
                            f"in {res.get('cpu_s', 0):.2f}s cpu" + (f" ({m.group(1)} differ)" if m else " -> kept"))
        if not m:
            continue
        differ = m.group(1)
        sel = p["selected"]
        waste = (cost_ms(pack(sel, dur), dur) - cost_ms(pack(sel - {tp}, dur), dur)) / 1000
        est2 = RUN_SCALE * (TPCDS_RUN_FIXED_S + TPCDS_RUN_PAIR_S * len(differ.split(",")))
        left = budget - (time.monotonic() - start)
        if waste * TPCDS_P_PASS_FEW - est2 < RUN_MARGIN_S or left < est2 * 1.5 + 1:
            p["reasons"].append("narrowed run not worth it -> kept")
            continue
        try:
            # a slow (possibly hanging) narrowed run times out and keeps the test
            r2 = runner.run_candidates(files, [TPCDS_ID], timeout_s=min(RUN_TIMEOUT_S, left, 2 * est2 + 2),
                                       extra_env={"ARGUS_TPCDS_ONLY": differ})
        except Exception as e:
            p["reasons"].append(f"narrowed run error {type(e).__name__} -> kept")
            continue
        if r2.get("failing") is not None and TPCDS_ID in r2.get("passed", ()):
            p["selected"] = sel - {tp}
            p["reasons"].append(f"narrowed run passed in {r2['cpu_s']:.2f}s cpu -> dropped")
        else:
            p["reasons"].append(f"narrowed run {r2.get('status')} in {r2.get('cpu_s', 0):.2f}s cpu -> kept")


def _use_result(p, res, nodeid_num):
    """Keeps only the tests the runner saw fail; any inconclusive run keeps the static set."""
    if res.get("failing") is None:
        p["reasons"].append(f"runner {res.get('status')} -> static")
        return
    failing = {nodeid_num[n] for n in res["failing"] if n in nodeid_num}
    p["reasons"].append(f"runner ran {len(p['selected'])} in {res.get('cpu_s', 0):.2f}s cpu -> {len(failing)} fail")
    p["selected"] = failing


def main():
    tests = json.load(open(os.path.join(DATA, "tests.json")))["tests"]
    dur = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    every = set(dur)
    nodeid_num = {t["id"]: t["number"] for t in tests}
    tid = {t["number"]: t["id"] for t in tests}
    tfile = {t["number"]: t["file"] for t in tests}
    tests_by_file = {}
    for t in tests:
        tests_by_file.setdefault(t["file"], []).append(t["number"])
    cov = Map(MAP)
    tp = nodeid_num.get(TPCDS_ID) if TP is not None else None
    cdir = os.path.join(DATA, "commits")
    plans = []
    for name in sorted(os.listdir(cdir)):
        if not name.endswith(".patch"):
            continue
        cid = name[:-len(".patch")]
        reasons = []
        text = ""
        check = False
        imports = []
        try:
            text = open(os.path.join(cdir, name), encoding="utf-8", errors="replace").read()
            seen = LineLog(cov, tp)
            selected = select(text, every, seen, tests_by_file, nodeid_num, reasons, imports) & every
        except Exception as e:  # never crash: a crash rejects the run; a mismatch must not miss
            selected = set(every)
            imports = []
            reasons.append(f"{type(e).__name__} {e} -> all")
        if tp is not None and tp in selected and selected != every:
            try:
                # narrowable only when line coverage alone selected it
                again = select(text, every, LineLog(cov, tp, drop=True), tests_by_file, nodeid_num, [], []) & every
                check = tp not in again and TP.clear(seen.queries, seen.fixture)
            except Exception as e:
                log(cid, "tpcds check planning error", repr(e))
            reasons.append(f"tpcds: {'checkable' if check else 'not checkable'}")
        plans.append({"cid": cid, "text": text, "selected": selected, "reasons": reasons,
                      "tpcds_test": tp, "tpcds_check": check, "imports": imports})
    try:
        import_checks(plans, every)
    except Exception as e:  # unproven narrowings must not stand
        log("import checks failed:", repr(e))
        for p in plans:
            if p.get("imports") and not any(r.startswith("import check ok") for r in p["reasons"]):
                p["selected"] = set(every)
                p["tpcds_check"] = False
    try:
        confirm_by_running(plans, dur, tfile, tid, nodeid_num, every)
    except Exception as e:  # the static selections stand
        log("runner phase failed:", repr(e))
    for p in plans:
        print(json.dumps({"commit": p["cid"], "workers": pack(p["selected"] & every, dur)}, separators=(",", ":")))
        log(p["cid"], "; ".join(p["reasons"]))


if __name__ == "__main__":
    main()
