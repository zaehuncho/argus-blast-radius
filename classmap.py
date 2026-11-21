"""Team Argus - class-attribute map: which tests can see an edit to a class body.

The map (map/classes.sqlite, built by tools/build_iso_classes.sh and
tools/make_classmap.py from the pinned commit only) records, for every test run
alone in a fresh process, each (class, attribute name) that Python's attribute
lookup resolved through a sqlglot class: instance reads, reads off the class
object, subclass and super() reads, method lookups. It also records which
class-attribute values were consumed while another class was being created
(class bodies, the _Dialect metaclass, __init_subclass__ hooks).

    cm = ClassMap("map/classes.sqlite")
    cm.users("sqlglot/dialects/tsql.py", "TSQL")                  -> set of test numbers
    cm.readers("sqlglot/dialects/tsql.py", "TSQL", "WEEK_OFFSET") -> set of test numbers
    v = cm.analyse(path, old_text, new_text)
        v.tests        set of tests for all class-level changes, or None = keep the broad rule
        v.func_ranges  [(lo, hi)] old-line body ranges of functions whose code changed; the
                       caller must add the tests whose coverage runs any of those lines
        v.extra        [(path, lo, hi, limit)] more coverage to add: the tests that run any of
                       those lines of `path`, intersected with `limit` unless it is None
                       (classmap.narrow() applies both)
        v.reason       short text for the log

The rule is deliberately narrow. It answers only when every changed statement is
  * a class-body assignment NAME = value whose change cannot raise at import
    (constants, dict/list/set/tuple entries, lambda bodies), or
  * a method whose signature changed, was added as an override, or was removed,
  * a function body (left to line coverage via func_ranges);
and nothing consumed the old value while classes were being built, other than
the three creation hooks we have read line by line (and then only for
same-type constant edits). A statement that differs only inside lambda bodies,
at class or module level, selects the tests that entered those lambdas.
Everything else returns None.
"""
import ast
import json
import os
import re
import sqlite3
import zlib
from collections import namedtuple

# extra = [(path, lo, hi, limit)]: add the tests whose coverage runs any of those lines of
# `path`, intersected with `limit` when it is a set. narrow() applies it.
# import_check: True when the tests are right only if the patched module still imports.
Verdict = namedtuple("Verdict", "tests reason func_ranges extra import_check", defaults=((), False))

# Facts about the creation hooks at the pinned commit, on top of the map's own table:
# hook -> attribute -> the attributes of the class being created that are computed from it.
# _TokenizerBase.__init_subclass__ uses cls.KEYWORDS for two things only: whether HINT_START
# is a keyword (an entry of cls._COMMENTS) and the keys of cls._KEYWORD_TRIE.
# _Dialect.__new__ uses the tokenizer's KEYWORDS only for `"(+)" in KEYWORDS`.
HOOK_DERIVED = {
    "sqlglot/tokens.py:_TokenizerBase.__init_subclass__": {"KEYWORDS": ["_COMMENTS", "_KEYWORD_TRIE"]},
    "sqlglot/dialects/dialect.py:_Dialect.__new__": {"KEYWORDS": ["SUPPORTS_COLUMN_JOIN_MARKS"]},
}

# Rule for constants reused as parameter defaults (`def f(x=NAME)` in the class body or
# `def f(x=C.NAME)` elsewhere). Off: such an edit keeps the broad rule, as any other reuse
# inside the class body does. When on, the edit selects the tests that run those defs'
# bodies plus the call-time readers, which relies on the FREE_LOOKUPS tables below.
DEFAULT_ARG_RULE = False

# Used only when DEFAULT_ARG_RULE is on.
# Every place at the pinned commit (sqlglot/ and tests/) where an attribute is looked up
# under a computed name on an object that could be of any class (tools/classmap_dynsites.py
# lists all computed-name lookups; the others are on modules, on Token slots, or only on
# names ending in "_sql"). (path, first line, last line, text expected on the first line)
FREE_LOOKUPS = [
    ("sqlglot/generators/duckdb.py", 1434, 1434, "getattr(annotated, return_value_attr)"),
    ("sqlglot/expressions/core.py", 2488, 2495, "return _Struct("),
    ("tests/test_schema.py", 429, 429, "getattr(schema, method)"),
]
# the same, inside a one-line lambda: the tests that called the lambda
FREE_LOOKUP_LAMBDAS = [("sqlglot/executor/env.py", 283, "getattr(e, this)")]

# A class attribute whose new value calls exactly the same functions as the old one
# (same callee expressions, same number of calls; no lambda or def text in between) is
# accepted when the caller proves the patched module still imports (Verdict.import_check):
# e.g. `CENTIMILLIS = Literal.number(5)` -> 6, or a comprehension filter `k not in (...)`
# -> `k in (...)`. Only the value differs then; who consumed it at class creation is
# still checked below (`strict` is required for that).
IMPORT_CHECK_RULE = True

# A class-body assignment TARGET = f(NAME) that reuses an edited attribute NAME (e.g.
# `SHOW_TRIE = new_trie(key.split(" ") for key in SHOW_PARSERS)`): TARGET may change
# too, so the readers of TARGET count as well (and TARGET's own reuses, recursively);
# requires the import check, since computing TARGET from the new value could raise.
DERIVED_RULE = True


def _callees(n):
    return sorted(_dump(x.func) for x in ast.walk(n) if isinstance(x, ast.Call))


def _same_callees(a, b):
    """Building the new value calls the same functions as the old one did: the same
    callee expressions, as many times; lambdas (whose bodies do not run while the
    value is built) must be identical on both sides; no walrus or yield."""
    if any(isinstance(x, (ast.NamedExpr, ast.Await, ast.Yield, ast.YieldFrom))
           for v in (a, b) for x in ast.walk(v)):
        return False

    def lambdas(n):
        return sorted(_dump(x) for x in ast.walk(n) if isinstance(x, ast.Lambda))

    return lambdas(a) == lambdas(b) and _callees(a) == _callees(b)


# Attributes whose edit changes state outside the classes involved:
# _Dialect.__new__ pops entries from the generator's TRANSFORMS dict based on it.
FORCE_BROAD = {"SUPPORTED_JSON_PATH_PARTS"}


class Broad(Exception):
    """This change is outside what the class map can bound."""


def module_of(path):
    m = path[:-3].replace("/", ".")
    return m[: -len(".__init__")] if m.endswith(".__init__") else m


def _dump(n):
    return ast.dump(n, include_attributes=False)


class _Texts:
    """Statement equality without dumping trees: two statements with the same source
    text (same indentation included) are the same statement. ast.dump is the fallback."""

    def __init__(self, old_text, new_text):
        self.old, self.new = old_text.split("\n"), new_text.split("\n")

    @staticmethod
    def _span(n):
        lo = min([n.lineno] + [d.lineno for d in getattr(n, "decorator_list", [])])
        return lo - 1, n.end_lineno

    def mentions(self, ca, cb, name):
        """False only if `name` occurs at most once as a word in each class's source
        (its own binding), so nothing else in the body can refer to it."""
        pat = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])")
        for lines, c in ((self.old, ca), (self.new, cb)):
            lo, hi = self._span(c)
            if sum(len(pat.findall(l)) for l in lines[lo:hi] if name in l) > 1:
                return True
        return False

    def same(self, a, b):
        if type(a) is not type(b):
            return False
        (alo, ahi), (blo, bhi) = self._span(a), self._span(b)
        if (
            ahi - alo == bhi - blo
            and a.col_offset == b.col_offset
            and a.end_col_offset == b.end_col_offset
            and self.old[alo:ahi] == self.new[blo:bhi]
        ):
            return True
        return _dump(a) == _dump(b)


def _key(s):
    if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return ("def", s.name)
    if isinstance(s, ast.ClassDef):
        return ("class", s.name)
    if isinstance(s, ast.Assign) and len(s.targets) == 1 and isinstance(s.targets[0], ast.Name):
        return ("attr", s.targets[0].id)
    if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name) and s.value is not None and s.simple:
        return ("attr", s.target.id)
    return None


def _class_header(n):
    return (
        n.name,
        [_dump(x) for x in n.bases],
        [_dump(x) for x in n.keywords],
        [_dump(x) for x in n.decorator_list],
        [_dump(x) for x in getattr(n, "type_params", [])],
    )


def _def_header(n):
    return (
        type(n).__name__,
        n.name,
        _dump(n.args),
        [_dump(x) for x in n.decorator_list],
        _dump(n.returns) if n.returns else None,
        [_dump(x) for x in getattr(n, "type_params", [])],
    )


def _body_range(n):
    return (n.body[0].lineno, n.end_lineno)


# ------------------------------------------------------------ what runs at import

def _import_level(node):
    """Yields the nodes evaluated when `node` (a module or class-body statement) runs
    at import: function and lambda bodies and nested class bodies are left out."""
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            stack.extend(n.decorator_list)
            stack.extend(n.args.defaults)
            stack.extend(d for d in n.args.kw_defaults if d is not None)
        elif isinstance(n, ast.Lambda):
            stack.extend(n.args.defaults)
            stack.extend(d for d in n.args.kw_defaults if d is not None)
        elif isinstance(n, ast.ClassDef):
            stack.extend(n.decorator_list)
            stack.extend(n.bases)
            stack.extend(k.value for k in n.keywords)
        else:
            stack.extend(ast.iter_child_nodes(n))


def _reuses(cls_node, name):
    """True if the class body mentions `name` anywhere it is evaluated at class
    creation, other than as the target of its own plain assignment."""
    own = set()
    for s in cls_node.body:
        if _key(s) == ("attr", name):
            own.add(id(s.targets[0] if isinstance(s, ast.Assign) else s.target))
    for s in cls_node.body:
        for n in _import_level(s):
            if isinstance(n, ast.Name) and n.id == name and id(n) not in own:
                return True
    return False


def _reuse_sites(cls_node, name):
    """How the class body uses `name` besides binding it. Returns a list of
    ("pop", key) for a statement `NAME.pop("key")` (key None for the two-argument form,
    which cannot fail) and ("default", def node) for an undecorated def whose parameter
    default is exactly NAME; or None if it is used in any other way."""
    own = set()
    for s in cls_node.body:
        if _key(s) == ("attr", name):
            own.add(id(s.targets[0] if isinstance(s, ast.Assign) else s.target))
    out = []
    for s in cls_node.body:
        uses = [n for n in _import_level(s) if isinstance(n, ast.Name) and n.id == name and id(n) not in own]
        if not uses:
            continue
        if (
            isinstance(s, ast.Expr)
            and isinstance(s.value, ast.Call)
            and isinstance(s.value.func, ast.Attribute)
            and s.value.func.attr == "pop"
            and s.value.func.value is uses[0]
            and len(uses) == 1
            and len(s.value.args) in (1, 2)
            and not s.value.keywords
            and all(isinstance(a, ast.Constant) for a in s.value.args)
        ):
            out.append(("pop", s.value.args[0].value if len(s.value.args) == 1 else None))
        elif isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)) and not s.decorator_list:
            defaults = s.args.defaults + [d for d in s.args.kw_defaults if d is not None]
            if not all(any(u is d for d in defaults) for u in uses):
                return None
            out.append(("default", s))
        elif (
            DERIVED_RULE
            and _key(s) is not None
            and _key(s)[0] == "attr"
            and _key(s)[1] != name
            and _same_callees(_value(s), _value(s))  # no lambda, walrus or yield in it
        ):
            # TARGET = f(NAME): TARGET is recomputed from the new value at class creation
            out.append(("derived", _key(s)[1]))
        else:
            return None
    return out


def _reuse_signature(sites):
    return [(k, v if k in ("pop", "derived") else _def_header(v)) for k, v in sites]


class _Sources:
    """The pinned sqlglot/ and tests/ sources, read once, to find where a name is mentioned."""

    def __init__(self, root):
        self.root, self.files = root, None

    def _load(self):
        self.files = {}
        for top in ("sqlglot", "tests"):
            for d, _, fs in os.walk(os.path.join(self.root, top)):
                for f in fs:
                    if f.endswith(".py"):
                        full = os.path.join(d, f)
                        try:
                            with open(full, "rb") as fh:
                                self.files[os.path.relpath(full, self.root).replace(os.sep, "/")] = fh.read()
                        except OSError:
                            pass

    def line(self, path, lineno):
        if self.files is None:
            self._load()
        data = self.files.get(path)
        if data is None:
            return ""
        lines = data.split(b"\n")
        return lines[lineno - 1].decode("utf-8", "replace") if lineno <= len(lines) else ""

    def maybe_default(self, name):
        """Cheap text test: could some def use `<something>.NAME` as a parameter default?
        Only decides whether mentions() is worth running."""
        if self.files is None:
            self._load()
        needle = name.encode()
        pat = re.compile(rb"=\s*[\w.]+\." + re.escape(needle) + rb"\s*[,)]")
        return any(needle in data and pat.search(data) for data in self.files.values())

    def mentions(self, name):
        """Every `x.NAME` and every string "NAME" in the sources, classified:
        ("default", path, lo, hi)  the default of an undecorated def that runs at import;
                                   lo..hi is the def's body
        ("call", path, lo, hi)     inside a def body; lo..hi is that body
        ("import", path, lo, hi)   anything else evaluated at import, lambda bodies included
        Returns None if a file that mentions the name does not parse."""
        if self.files is None:
            self._load()
        needle = name.encode()
        out = []
        for path, data in self.files.items():
            if needle not in data:
                continue
            try:
                tree = ast.parse(data)
            except (SyntaxError, ValueError):
                return None

            def hit(n):
                return (isinstance(n, ast.Attribute) and n.attr == name and isinstance(n.ctx, ast.Load)) or (
                    isinstance(n, ast.Constant) and n.value == name
                )

            def walk(node, body):
                # body = (lo, hi) of the innermost enclosing def body, None at import level
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        defaults = child.args.defaults + [d for d in child.args.kw_defaults if d is not None]
                        rng = _body_range(child)
                        for d in defaults:
                            if hit(d) and body is None and not child.decorator_list:
                                out.append(("default", path, rng[0], rng[1]))
                            else:
                                visit(d, body)
                        for x in child.decorator_list:
                            visit(x, body)
                        for a in ast.walk(child.args):
                            if isinstance(a, ast.arg) and a.annotation is not None:
                                visit(a.annotation, body)
                        if child.returns is not None:
                            visit(child.returns, body)
                        for x in child.body:
                            visit(x, rng)
                    else:
                        visit(child, body)

            def visit(n, body):
                if hit(n):
                    if body is None:
                        out.append(("import", path, n.lineno, n.end_lineno))
                    else:
                        out.append(("call", path, body[0], body[1]))
                walk(n, body)

            walk(tree, None)
        return out


class _Proven:
    """Attribute chains (exp.Foo, TokenType.X) the old module evaluates unconditionally
    at import, so evaluating them again elsewhere cannot raise."""

    def __init__(self, tree):
        self._tree, self._sets = tree, None

    @property
    def any(self):
        return self._build()[0]

    @property
    def hashable(self):
        return self._build()[1]

    def _build(self):
        if self._sets is not None:
            return self._sets
        self._any, self._hashable = set(), set()
        tree = self._tree
        local = set()  # names bound in some class body: they may shadow a module global
        for n in ast.walk(tree):
            if not isinstance(n, ast.ClassDef):
                continue
            for s in n.body:
                if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    local.add(s.name)
                    continue
                for x in ast.walk(s):
                    if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store):
                        local.add(x.id)
                    elif isinstance(x, ast.alias):
                        local.add((x.asname or x.name).split(".")[0])
        self._local = local
        self._walk(tree, False)
        self._sets = (self._any, self._hashable)
        return self._sets

    def _root(self, n):
        while isinstance(n, ast.Attribute):
            n = n.value
        return n.id if isinstance(n, ast.Name) else None

    def _walk(self, n, hashpos):
        if isinstance(n, (ast.If, ast.Try, ast.While, ast.For, ast.With, ast.IfExp, ast.BoolOp,
                          ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp, ast.Match)):
            return  # may not run
        if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load):
            root = self._root(n)
            if root is not None and root not in self._local:
                self._any.add(_dump(n))
                if hashpos:
                    self._hashable.add(_dump(n))
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kids = n.decorator_list + n.args.defaults + [d for d in n.args.kw_defaults if d is not None]
            for k in kids:
                self._walk(k, False)
        elif isinstance(n, ast.Lambda):
            for k in n.args.defaults + [d for d in n.args.kw_defaults if d is not None]:
                self._walk(k, False)
        elif isinstance(n, ast.Dict):
            for k in n.keys:
                if k is not None:
                    self._walk(k, True)
            for v in n.values:
                self._walk(v, False)
        elif isinstance(n, ast.Set):
            for e in n.elts:
                self._walk(e, True)
        elif isinstance(n, ast.Tuple):
            for e in n.elts:
                self._walk(e, hashpos)
        else:
            for k in ast.iter_child_nodes(n):
                self._walk(k, False)


_NUM = (int, float)


def _is_num(n):
    return isinstance(n, ast.Constant) and isinstance(n.value, _NUM) and not isinstance(n.value, bool)


def _inert(n, P, hashpos=False):
    """Evaluating `n` at import cannot raise and has no effect."""
    if isinstance(n, ast.Constant):
        return True
    if isinstance(n, ast.Attribute):
        return _dump(n) in (P.hashable if hashpos else P.any)
    if isinstance(n, ast.Lambda):
        return not n.args.defaults and not any(n.args.kw_defaults)
    if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd)):
        return _is_num(n.operand)
    if isinstance(n, ast.Tuple):
        return all(_inert(e, P, hashpos) for e in n.elts)
    if hashpos:
        return False
    if isinstance(n, ast.List):
        return all(_inert(e, P) for e in n.elts)
    if isinstance(n, ast.Set):
        return all(_inert(e, P, True) for e in n.elts)
    if isinstance(n, ast.Dict):
        return all(k is not None and _inert(k, P, True) for k in n.keys) and all(_inert(v, P) for v in n.values)
    return False


_DISPLAY = (ast.Dict, ast.List, ast.Set, ast.Tuple)
_SET_OPS = (ast.Sub, ast.BitOr, ast.BitAnd, ast.BitXor, ast.Add)


def _safe(a, b, P, free=True, hashpos=False):
    """True when replacing expression `a` by `b` cannot make the import raise or
    have a new side effect, given that `a` evaluated fine. `free` is False for
    operands of a binary operator, where only container displays may differ."""
    if _dump(a) == _dump(b):
        return True
    if isinstance(a, _DISPLAY) and type(a) is type(b):
        if hashpos and not isinstance(a, ast.Tuple):
            return False
        if isinstance(a, ast.Dict):
            old = {}
            for k, v in zip(a.keys, a.values):
                kd = ("**", _dump(v)) if k is None else ("k", _dump(k))
                if kd in old:
                    return False
                old[kd] = v
            for k, v in zip(b.keys, b.values):
                kd = ("**", _dump(v)) if k is None else ("k", _dump(k))
                if kd in old:
                    if not _safe(old[kd], v, P):
                        return False
                elif k is None or not _inert(k, P, True) or not _inert(v, P):
                    return False
            return True
        eh = hashpos or isinstance(a, ast.Set)
        if len(a.elts) == len(b.elts):
            return all(_safe(x, y, P, True, eh) for x, y in zip(a.elts, b.elts))
        have = {_dump(x) for x in a.elts}
        return all(_dump(y) in have or _inert(y, P, eh) for y in b.elts)
    if isinstance(a, ast.BinOp) and isinstance(b, ast.BinOp) and type(a.op) is type(b.op) and isinstance(a.op, _SET_OPS):
        return _safe(a.left, b.left, P, False) and _safe(a.right, b.right, P, False)
    if not free:
        return False
    if isinstance(a, ast.Constant) and isinstance(b, ast.Constant):
        return True
    if isinstance(b, ast.Attribute) and isinstance(a, (ast.Attribute, ast.Constant)):
        return _inert(b, P, hashpos)
    if isinstance(a, ast.Lambda) and isinstance(b, ast.Lambda):
        return _dump(a.args) == _dump(b.args)  # the body runs only when called
    if isinstance(a, ast.UnaryOp) and isinstance(b, ast.UnaryOp) and type(a.op) is type(b.op):
        return _is_num(a.operand) and _is_num(b.operand)
    return False


def _strict(a, b):
    """Same shape everywhere; only constants differ, each keeping its type, and no
    string becomes empty. This is what the creation hooks were checked against."""
    if type(a) is not type(b):
        return False
    if isinstance(a, ast.Constant):
        if type(a.value) is not type(b.value):
            return False
        if isinstance(b.value, (str, bytes)) and not b.value and a.value:
            return False
        return True
    for (_, va), (_, vb) in zip(ast.iter_fields(a), ast.iter_fields(b)):
        if isinstance(va, list):
            if not isinstance(vb, list) or len(va) != len(vb):
                return False
            for x, y in zip(va, vb):
                if isinstance(x, ast.AST):
                    if not isinstance(y, ast.AST) or not _strict(x, y):
                        return False
                elif x != y:
                    return False
        elif isinstance(va, ast.AST):
            if not isinstance(vb, ast.AST) or not _strict(va, vb):
                return False
        elif va != vb:
            return False
    return True


def _lambda_only(a, b, out):
    """True when `a` and `b` differ only inside lambda bodies (same parameters and
    defaults); the changed lambdas of `a` are appended to `out`."""
    if type(a) is not type(b):
        return False
    if isinstance(a, ast.Lambda):
        if _dump(a) == _dump(b):
            return True
        if _dump(a.args) != _dump(b.args):
            return False
        out.append(a)
        return True
    for (_, va), (_, vb) in zip(ast.iter_fields(a), ast.iter_fields(b)):
        if isinstance(va, list):
            if not isinstance(vb, list) or len(va) != len(vb):
                return False
            for x, y in zip(va, vb):
                if isinstance(x, ast.AST):
                    if not isinstance(y, ast.AST) or not _lambda_only(x, y, out):
                        return False
                elif x != y:
                    return False
        elif isinstance(va, ast.AST):
            if not isinstance(vb, ast.AST) or not _lambda_only(va, vb, out):
                return False
        elif va != vb:
            return False
    return True


def _value(s):
    return s.value


# ------------------------------------------------------------ the map

class ClassMap:
    def __init__(self, path):
        self.db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        self.cls = {}
        self._by_id = {}
        q = "select id, name, tracked, mro, names, fn, un from classes"
        for i, name, tracked, mro, names, fn, un in self.db.execute(q):
            self.cls[name] = {
                "id": i, "tracked": bool(tracked), "mro": json.loads(mro),
                "names": set(json.loads(names)), "fn": set(json.loads(fn)), "un": set(json.loads(un)),
            }
            self._by_id[i] = name
        meta = dict(self.db.execute("select key, value from meta"))
        self.hooks = json.loads(meta["hooks"])          # reader -> {"owner": class, "names": [...]}
        self.always = set(json.loads(meta["always"]))    # tests with no usable record
        self.nofunc = set(json.loads(meta["nofunc"]))    # tests recorded with data attributes only
        self.ntests = int(meta["ntests"])
        self.no_lambda = set(json.loads(meta["no_lambda"]))  # tests whose lambda record is unusable
        for hook, table in HOOK_DERIVED.items():
            if hook in self.hooks:
                merged = dict(table)
                merged.update(self.hooks[hook].get("derived", {}))
                self.hooks[hook]["derived"] = merged
        self._blobs = {}
        self._sources = _Sources(os.environ.get("ARGUS_SQLGLOT", "/app/sqlglot"))
        self._extra, self._path = [], None

    # -- raw lookups
    def _tests(self, bid):
        if bid not in self._blobs:
            (b,) = self.db.execute("select bits from blobs where id=?", (bid,)).fetchone()
            bits = zlib.decompress(b)
            self._blobs[bid] = {i * 8 + j for i, byte in enumerate(bits) if byte for j in range(8) if byte >> j & 1}
        return self._blobs[bid]

    def _readers(self, cname, attr):
        c = self.cls.get(cname)
        if c is None:
            return set()
        row = self.db.execute("select blob_id from pairs where class_id=? and attr=?", (c["id"], attr)).fetchone()
        return set(self._tests(row[0])) if row else set()

    def _users(self, cname):
        c = self.cls.get(cname)
        if c is None:
            return set()
        row = self.db.execute("select blob_id from users where class_id=?", (c["id"],)).fetchone()
        return set(self._tests(row[0])) if row else set()

    def _edges(self, cname, attr=None):
        """[(class K whose creation read it, reader function)]"""
        c = self.cls.get(cname)
        if c is None:
            return []
        if attr is None:
            q = self.db.execute("select distinct k_id, reader from edges where class_id=?", (c["id"],))
        else:
            q = self.db.execute("select distinct k_id, reader from edges where class_id=? and attr=?", (c["id"], attr))
        return [(self._by_id[k], r) for k, r in q]

    def entered(self, path, line):
        """Tests that called a lambda whose `lambda` keyword is on this line."""
        row = self.db.execute("select blob_id from lambdas where path=? and line=?", (path, line)).fetchone()
        return (set(self._tests(row[0])) if row else set()) | self.no_lambda

    # -- public lookups
    def known(self, path, qualname):
        c = self.cls.get(f"{module_of(path)}:{qualname}")
        return bool(c and c["tracked"])

    def users(self, path, qualname):
        """Tests that read any attribute (data or method) through this class, a
        subclass or an instance of either. None if the class is not tracked."""
        name = f"{module_of(path)}:{qualname}"
        if not self.known(path, qualname):
            return None
        return self._users(name) | self.always

    def readers(self, path, qualname, attr):
        """Tests whose lookup of `attr` went through this class and was not already
        answered by a subclass. None if the class is not tracked."""
        name = f"{module_of(path)}:{qualname}"
        if not self.known(path, qualname):
            return None
        return self._readers(name, attr) | self.always

    # -- the rule
    def _hooks_reading(self, cname, attr):
        """Vetted creation hooks that apply to this class and may read `attr` off it."""
        line = [cname] + self.cls[cname]["mro"]
        return [key for key, h in self.hooks.items() if h["owner"] in line and attr in h["names"]]

    def _taint(self, cname, tests, seen):
        """Class `cname` was rebuilt from changed data by a vetted hook: any of its own
        attributes may differ, and so may whatever was built from them."""
        if cname in seen:
            return
        seen.add(cname)
        c = self.cls.get(cname)
        if c is None or not c["tracked"]:
            raise Broad(f"value consumed while creating untracked class {cname}")
        tests |= self._users(cname)
        for k, reader in self._edges(cname):
            if k == cname:
                continue
            if reader not in self.hooks:
                raise Broad(f"{cname} is consumed by {reader} while creating {k}")
            self._taint(k, tests, seen)

    def _consume(self, k, hook, src, tests, seen):
        """Hook `hook` read attribute `src` while creating class `k`. If we know exactly
        which attributes of `k` it computes from `src`, follow those; else taint `k`."""
        derived = self.hooks[hook].get("derived", {}).get(src)
        c = self.cls.get(k)
        if c is None or not c["tracked"]:
            raise Broad(f"value consumed while creating untracked class {k}")
        if derived is not None and self.hooks[hook]["owner"] not in [k] + c["mro"]:
            # The hook ran for a class made inside k's creation (a dialect's default
            # Tokenizer): what it computed is stored on that class, not on k.
            derived = None
        if derived is None:
            self._taint(k, tests, seen)
            return
        for r in derived:
            if (k, r) in seen:
                continue
            seen.add((k, r))
            tests |= self._readers(k, r)
            for k2, reader in self._edges(k, r):
                if reader not in self.hooks:
                    raise Broad(f"{k}.{r} is consumed by {reader} while creating {k2}")
                self._consume(k2, reader, r, tests, seen)

    def _refine(self, cname, name, readers):
        """Recorded readers include every test whose process ran an import-time read. When
        the only import-time reads of `name` are parameter defaults (`def f(x=C.NAME)`),
        the value matters to a test only if it runs such a def's body, or really reads the
        attribute at call time. Returns the tests for the call-time reads and queues the
        coverage lookups in self._extra; None if the import-time reads are anything else
        (the caller then keeps all recorded readers)."""
        if not DEFAULT_ARG_RULE:
            return None
        if name.endswith("_sql") or cname.startswith("sqlglot.tokenizer_core:"):
            return None  # names and classes the computed-name lookups can reach
        sites = self._sources.mentions(name)
        if sites is None or not any(k == "default" for k, *_ in sites):
            return None
        if any(k == "import" for k, *_ in sites):
            return None
        for path, lo, _, text in FREE_LOOKUPS:
            if text not in self._sources.line(path, lo):
                return None  # not the sources this table was made from
        for path, line, text in FREE_LOOKUP_LAMBDAS:
            if text not in self._sources.line(path, line):
                return None
        got = set()
        for path, line, _ in FREE_LOOKUP_LAMBDAS:
            got |= self.entered(path, line) & readers
        for kind, path, lo, hi in set(sites):
            self._extra.append((path, lo, hi, None if kind == "default" else readers))
        for path, lo, hi, _ in FREE_LOOKUPS:
            self._extra.append((path, lo, hi, readers))
        return got

    def _derive(self, cname, ca, cb, target, tests, seen):
        """Class attribute `target` is computed in the class body from an edited one, so
        it may differ too: its readers count, and so do attributes computed from it."""
        if ("derived", cname, target) in seen:
            return
        seen.add(("derived", cname, target))
        c = self.cls.get(cname)
        if target.startswith("__") or target in FORCE_BROAD or target not in c["names"]:
            raise Broad(f"{cname}.{target}: derived attribute not tracked")
        if self._hooks_reading(cname, target) or self._edges(cname, target):
            raise Broad(f"{cname}.{target}: derived and read while classes are created")
        tests |= self._readers(cname, target)
        if self._T.mentions(ca, cb, target):
            ra, rb = _reuse_sites(ca, target), _reuse_sites(cb, target)
            if ra is None or rb is None or _reuse_signature(ra) != _reuse_signature(rb):
                raise Broad(f"{cname}.{target}: derived attribute used again inside the class body")
            for kd, t2 in ra:
                if kd != "derived":
                    raise Broad(f"{cname}.{target}: derived attribute used again inside the class body")
                self._derive(cname, ca, cb, t2, tests, seen)

    def _defined_above(self, cname, attr):
        """True if lookups of `attr` that pass this class are answered by a tracked
        attribute of the nearest base that has one (so they are all on record)."""
        for b in self.cls[cname]["mro"]:
            c = self.cls.get(b)
            if c is None:
                return False
            if attr in c["names"]:
                return True
            if attr in c["un"]:
                return False
        return False

    def _edit(self, cname, kind, name, sa, sb, ca, cb, P, tests, seen):
        c = self.cls.get(cname)
        if c is None or not c["tracked"]:
            raise Broad(f"class {cname} is not tracked")
        if name.startswith("__") and name.endswith("__"):
            raise Broad(f"{cname}.{name}: special name")
        if name in FORCE_BROAD:
            raise Broad(f"{cname}.{name}: changes state outside the class")
        if sa is not None and name not in c["names"]:
            raise Broad(f"{cname}.{name}: not a tracked attribute")
        if sa is None and not self._defined_above(cname, name):
            raise Broad(f"{cname}.{name}: new name, lookups that used to fail are not recorded")
        default_defs = []
        if self._T.mentions(ca, cb, name):
            ra, rb = _reuse_sites(ca, name), _reuse_sites(cb, name)
            if ra is None or rb is None or _reuse_signature(ra) != _reuse_signature(rb):
                raise Broad(f"{cname}.{name}: used again inside the class body")
            if ra:
                # Only two reuses are understood, and only for a changed plain value.
                if kind != "attr" or sa is None or sb is None:
                    raise Broad(f"{cname}.{name}: used again inside the class body")
                popped = {k for kd, k in ra if kd == "pop" and k is not None}
                if any(kd == "pop" for kd, _ in ra):
                    # NAME.pop("key") right after the literal: it must still find its key
                    va, vb = _value(sa), _value(sb)
                    if not (isinstance(va, ast.Dict) and isinstance(vb, ast.Dict)):
                        raise Broad(f"{cname}.{name}: popped in the class body and not a dict literal")
                    keys_a = {k.value for k in va.keys if isinstance(k, ast.Constant)}
                    keys_b = {k.value for k in vb.keys if isinstance(k, ast.Constant)}
                    if (popped & keys_a) - keys_b:
                        raise Broad(f"{cname}.{name}: a key popped in the class body was edited")
                default_defs = [d for kd, d in ra if kd == "default"]
                if default_defs and not DEFAULT_ARG_RULE:
                    raise Broad(f"{cname}.{name}: used again inside the class body")
                for kd, target in ra:
                    if kd == "derived":
                        self._import_check = True
                        self._derive(cname, ca, cb, target, tests, seen)
        strict = False
        if kind == "attr":
            if sa is not None and sb is not None:
                if isinstance(sa, ast.AnnAssign) != isinstance(sb, ast.AnnAssign) or (
                    isinstance(sa, ast.AnnAssign) and _dump(sa.annotation) != _dump(sb.annotation)
                ):
                    raise Broad(f"{cname}.{name}: annotation changed")
                strict = _strict(_value(sa), _value(sb))
                if not _safe(_value(sa), _value(sb), P):
                    if not (IMPORT_CHECK_RULE and _same_callees(_value(sa), _value(sb))):
                        raise Broad(f"{cname}.{name}: new value may fail or act at import")
                    # The same functions are called while the value is built: the caller
                    # must prove the module still imports (the value is then just another object).
                    self._import_check = True
            elif sb is not None:
                if isinstance(sb, ast.AnnAssign) or not _inert(_value(sb), P):
                    raise Broad(f"{cname}.{name}: added value may fail or act at import")
        else:
            if sb is not None:
                old_bits = set()
                if sa is not None:
                    if [_dump(x) for x in sa.decorator_list] != [_dump(x) for x in sb.decorator_list]:
                        raise Broad(f"{cname}.{name}: decorators changed")
                    for x in ast.walk(sa.args):
                        old_bits.add(_dump(x))
                    if sa.returns:
                        old_bits.add(_dump(sa.returns))
                elif sb.decorator_list:
                    raise Broad(f"{cname}.{name}: added method has decorators")
                if type(sa) is not type(sb) and sa is not None:
                    raise Broad(f"{cname}.{name}: def kind changed")
                evaluated = list(sb.args.defaults) + [d for d in sb.args.kw_defaults if d is not None]
                for a in sb.args.posonlyargs + sb.args.args + sb.args.kwonlyargs + [sb.args.vararg, sb.args.kwarg]:
                    if a is not None and a.annotation is not None:
                        evaluated.append(a.annotation)
                if sb.returns:
                    evaluated.append(sb.returns)
                if getattr(sb, "type_params", None):
                    raise Broad(f"{cname}.{name}: type parameters")
                for e in evaluated:
                    if _dump(e) not in old_bits and not _inert(e, P):
                        raise Broad(f"{cname}.{name}: signature may fail at import")
        own_hooks = self._hooks_reading(cname, name)
        edges = self._edges(cname, name)
        if own_hooks or edges:
            if not strict:
                raise Broad(f"{cname}.{name}: read while classes are created, and not a plain constant edit")
            for k, reader in edges:
                if reader not in self.hooks:
                    raise Broad(f"{cname}.{name}: consumed by {reader} while creating {k}")
            for h in own_hooks:
                self._consume(cname, h, name, tests, seen)
            for k, reader in edges:
                self._consume(k, reader, name, tests, seen)
        readers = self._readers(cname, name)
        if default_defs and (own_hooks or edges):
            raise Broad(f"{cname}.{name}: a parameter default and read while classes are created")
        for d in default_defs:
            # the value was copied into the def's defaults: whoever runs its body may use it
            lo, hi = _body_range(d)
            self._extra.append((self._path, lo, hi, None))
        refined = None
        if kind == "attr" and sa is not None and sb is not None and not own_hooks and not edges:
            if DEFAULT_ARG_RULE and (default_defs or self._sources.maybe_default(name)):
                refined = self._refine(cname, name, readers)
        if refined is None:
            tests |= readers
        else:
            tests |= refined
        callable_attr = (
            kind == "def"
            or name in c["fn"]
            or any(name in self.cls[b]["fn"] for b in c["mro"] if b in self.cls)
            or (sb is not None and kind == "attr" and isinstance(_value(sb), ast.Lambda))
        )
        if callable_attr and not (kind == "def" and sa is not None):
            # These tests were recorded without method lookups. For a changed or removed
            # def they are found through line coverage of its old body (func_ranges).
            tests |= self.nofunc

    def _diff_class(self, a, b, qual, mod, P, edits, funcs, lams, T):
        # statements that are not a plain assignment, def or class ("other"), and names
        # bound more than once, must be unchanged and in the same places
        def items(n):
            count = {}
            for s in n.body:
                k = _key(s)
                count[k] = count.get(k, 0) + 1
            return [(k if k and count[k] == 1 else None, s) for s in n.body for k in (_key(s),)]

        ia, ib = items(a), items(b)
        oa, ob = [s for k, s in ia if k is None], [s for k, s in ib if k is None]
        if len(oa) != len(ob) or not all(T.same(x, y) for x, y in zip(oa, ob)):
            raise Broad(f"{qual}: a class-level statement that is not a plain assignment or def changed")
        da = {k: s for k, s in ia if k is not None}
        db = {k: s for k, s in ib if k is not None}
        # order: every statement present on both sides keeps its place relative to the others
        n_other = iter(range(len(oa)))
        ka = [k if k is not None else ("other", next(n_other)) for k, _ in ia]
        n_other = iter(range(len(ob)))
        kb = [k if k is not None else ("other", next(n_other)) for k, _ in ib]
        sa_, sb_ = set(ka), set(kb)
        if [k for k in ka if k in sb_] != [k for k in kb if k in sa_]:
            raise Broad(f"{qual}: class-level statements reordered")
        for k in list(dict.fromkeys(ka + kb)):
            if k[0] == "other":
                continue
            sa, sb = da.get(k), db.get(k)
            if sa is not None and sb is not None and T.same(sa, sb):
                continue
            kind, name = k
            if kind == "class":
                if sa is None or sb is None or _class_header(sa) != _class_header(sb):
                    raise Broad(f"{qual}.{name}: nested class added, removed or re-based")
                self._diff_class(sa, sb, f"{qual}.{name}", mod, P, edits, funcs, lams, T)
            elif kind == "def" and sa is not None and sb is not None and _def_header(sa) == _def_header(sb):
                funcs.append(_body_range(sa))
            else:
                changed = []
                if sa is not None and sb is not None and _lambda_only(sa, sb, changed):
                    lams.extend(changed)  # nothing but code that runs when the lambda is called
                    continue
                if kind == "def" and sa is not None:
                    funcs.append(_body_range(sa))
                edits.append((f"{mod}:{qual}", kind, name, sa, sb, a, b))

    def diff(self, path, old_text, new_text, old, new, funcs=None):
        """Structural diff of two parsed versions of a file. Returns (edits, funcs, lams, P):
        edits = [(class name, "attr" | "def", name, old stmt, new stmt, old class, new class)],
        funcs = old-line body ranges of functions whose code changed, lams = changed lambdas.
        Raises Broad when anything else changed."""
        mod = module_of(path)
        self._path, self._extra = path, []
        edits, lams = [], []
        funcs = [] if funcs is None else funcs
        if len(old.body) != len(new.body):
            raise Broad("module-level statement added or removed")
        P = _Proven(old)
        T = self._T = _Texts(old_text, new_text)
        for a, b in zip(old.body, new.body):
            if T.same(a, b):
                continue
            if isinstance(a, ast.ClassDef) and isinstance(b, ast.ClassDef) and _class_header(a) == _class_header(b):
                self._diff_class(a, b, a.name, mod, P, edits, funcs, lams, T)
            elif (
                isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef))
                and type(a) is type(b)
                and _def_header(a) == _def_header(b)
            ):
                funcs.append(_body_range(a))
            else:
                changed = []
                if not _lambda_only(a, b, changed):
                    raise Broad("module-level statement changed")
                lams.extend(changed)
        return edits, funcs, lams, P

    def analyse(self, path, old_text, new_text, old_tree=None, new_tree=None):
        """See the module docstring. Never raises for a parseable pair of texts.
        Pass the parsed trees if you have them: parsing is most of the cost."""
        try:
            old = old_tree if old_tree is not None else ast.parse(old_text)
            new = new_tree if new_tree is not None else ast.parse(new_text)
        except SyntaxError:
            return Verdict(None, "does not parse", [])
        funcs = []
        self._import_check = False
        try:
            edits, funcs, lams, P = self.diff(path, old_text, new_text, old, new, funcs)
            tests, seen, why = set(), set(), []
            for cname, kind, name, sa, sb, ca, cb in edits:
                self._edit(cname, kind, name, sa, sb, ca, cb, P, tests, seen)
                why.append(f"{cname.split(':')[1]}.{name}")
        except Broad as e:
            return Verdict(None, str(e), funcs)
        except RecursionError:
            return Verdict(None, "too deep", funcs)
        if edits:
            tests |= self.always
        for lam in lams:
            tests |= self.entered(path, lam.lineno)
        parts = []
        if why:
            parts.append("class attrs " + ",".join(why))
        if lams:
            parts.append("lambdas at " + ",".join(str(l.lineno) for l in lams))
        reason = "; ".join(parts) or "function bodies only"
        if self._import_check:
            reason += " (if it imports)"
        return Verdict(tests, reason, funcs, tuple(self._extra), self._import_check)


def narrow(cm, cov, path, old_text, new_text, old_tree=None, new_tree=None):
    """For a file the selector would treat as 'module level' (every test that
    loads the module): returns (tests, reason) to use instead, or None to keep
    the broad rule. `cov` is the selector's coverage Map; functions whose code
    changed are covered by every test that runs any line of their body.
    Union the result with the selector's own function-body tests."""
    v = cm.analyse(path, old_text, new_text, old_tree, new_tree)
    if v.tests is None:
        return None
    tests = set(v.tests)
    for lo, hi in v.func_ranges:
        tests |= cov.tests_covering(path, range(lo, hi + 1))
    for p, lo, hi, limit in v.extra:
        got = cov.tests_covering(p, range(lo, hi + 1))
        tests |= got if limit is None else got & limit
    return tests, v.reason, v.import_check
