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
        narrowed = None
        if CM is not None:
            try:
                narrowed = classmap.narrow(CM, cov, path, old_text, new_text, old_tree, new_tree)
            except Exception as e:  # the broad rule stands
                log("classmap error", path, repr(e))
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


def select(text, every, cov, tests_by_file, nodeid_num, reasons):
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


def cost_ms(workers, dur):
    workers = [w for w in workers if w]
    if not workers:
        return 0
    return max(sum(dur[t] for t in w) for w in workers) + WORKER_COST_MS * len(workers)


# Runner CPU model from runner.py's measurements (fixed s, s per recorded s), scaled
# by 1.5: the scorer's machine used ~22 s of runner CPU on a batch where ours
# predicted roughly two thirds of that.
RUN_SCALE = 1.5
RUN_MODEL = {k: (f * RUN_SCALE, p * RUN_SCALE) for k, (f, p) in
             {"plain": (0.45, 1.0), "optimizer": (1.25, 1.4), "executor": (2.2, 2.0)}.items()}
RUN_MARGIN_S = 1.0          # run only if predicted CPU is this much below the static cost
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
    for gain, est, p in order:
        left = budget - (time.monotonic() - start)
        if left < est * 1.5 + 1:
            continue
        try:
            files = runner.files_from_patch(p["text"])
            res = runner.run_candidates(files, sorted(tid[t] for t in p["selected"]),
                                        timeout_s=min(RUN_TIMEOUT_S, left))
        except Exception as e:
            p["reasons"].append(f"runner error {type(e).__name__} -> static")
            continue
        if res.get("failing") is None:
            p["reasons"].append(f"runner {res.get('status')} -> static")
            continue
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
    cdir = os.path.join(DATA, "commits")
    plans = []
    for name in sorted(os.listdir(cdir)):
        if not name.endswith(".patch"):
            continue
        cid = name[:-len(".patch")]
        reasons = []
        text = ""
        try:
            text = open(os.path.join(cdir, name), encoding="utf-8", errors="replace").read()
            selected = select(text, every, cov, tests_by_file, nodeid_num, reasons) & every
        except Exception as e:  # never crash: a crash rejects the run; a mismatch must not miss
            selected = set(every)
            reasons.append(f"{type(e).__name__} {e} -> all")
        plans.append({"cid": cid, "text": text, "selected": selected, "reasons": reasons})
    try:
        confirm_by_running(plans, dur, tfile, tid, nodeid_num, every)
    except Exception as e:  # the static selections stand
        log("runner phase failed:", repr(e))
    for p in plans:
        print(json.dumps({"commit": p["cid"], "workers": pack(p["selected"] & every, dur)}, separators=(",", ":")))
        log(p["cid"], "; ".join(p["reasons"]))


if __name__ == "__main__":
    main()
