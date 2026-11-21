"""Loaded at interpreter start (on PYTHONPATH) during the isolated class-read pass.

Records which class attributes of sqlglot classes this process reads, so an
edit to one class-body statement can be mapped to the tests that can see it.

How: builtins.__build_class__ is wrapped. Right after a class defined under
sqlglot/ is created, every plain entry of its __dict__ (data, functions,
lambdas, classmethods, staticmethods) is replaced by a non-data descriptor that
returns the original value and notes (class the lookup went through, name).
Python's attribute lookup calls the descriptor for instance, class, subclass
and super() reads alike, so nothing has to be called on an instance for a read
to count. Dunders, properties/slots (data descriptors) and nested classes are
left alone.

Reads made while another sqlglot class is being created (its body, metaclass
__new__, __init_subclass__) are not credited to the test: they are stored as
edges "class K's creation consumed (owner, name)" together with the function
that read it, because the value was copied into K before any test ran.

The same pass notes which lambdas under sqlglot/ the process enters
(sys.monitoring PY_START, one report per code object).

Each process (spawned workers included) writes
$ARGUS_CLS_DIR/<test>.<pid>.json.z at exit, and an empty <test>.<pid>.start
when it begins, so a process that died without a record can be told apart.
"""
import atexit
import builtins
import os
import sys

_dir = os.environ.get("ARGUS_CLS_DIR")
_n = os.environ.get("ARGUS_TEST_NUM")


def _install():
    import json
    import zlib

    root = os.path.abspath(os.environ.get("ARGUS_ROOT", "/work/sqlglot"))
    src = os.path.join(root, "sqlglot") + os.sep
    try:
        # "module:qualname" of classes that must not be wrapped (decorated, etc.)
        bad = set(json.load(open(os.environ["ARGUS_CLS_SKIP"])))
    except Exception:
        bad = set()

    # second pass for tests that break when methods are wrapped (mock autospec):
    # wrap data only, so those tests still get exact data-attribute reads
    nofunc = bool(os.environ.get("ARGUS_CLS_NOFUNC"))
    orig_bc = builtins.__build_class__
    orig_imp = builtins.__import__
    getframe = sys._getframe

    ctx = []        # stack: a set while a sqlglot class is being created, None inside an import
    keep = {}       # id(class) -> class, for everything we refer to by id
    info = {}       # id(class) -> dict(id=..., tracked=bool, trackers=[...])
    known_ids = {}  # class id string -> count
    edges = {}      # id(K) -> set of (id(owner), name, id(definer), reader file, reader qualname)

    class T:
        __slots__ = ("v", "n", "k", "s", "g")

        def __get__(self, obj, owner=None):
            if owner is None:
                owner = type(obj)
            if ctx and ctx[-1] is not None:
                f = getframe(1)
                keep[id(owner)] = owner
                ctx[-1].add((id(owner), self.n, self.k, f.f_code.co_filename, f.f_code.co_qualname))
            else:
                self.s[id(owner)] = owner
            g = self.g
            if g is None:
                return self.v
            return g(self.v, obj, owner)

    def wrap(K, kid):
        trackers, plain = [], []
        for name, v in list(K.__dict__.items()):
            if name.startswith("__") and name.endswith("__"):
                continue
            tv = type(v)
            if tv is T:
                continue
            if isinstance(v, type) or hasattr(tv, "__set__") or hasattr(tv, "__delete__") or (nofunc and hasattr(tv, "__get__")):
                plain.append(name)  # nested class, property, slot: reads of it are not seen
                continue
            t = T()
            t.v, t.n, t.k, t.s = v, name, id(K), {}
            t.g = getattr(tv, "__get__", None)
            try:
                type.__setattr__(K, name, t)
            except Exception:
                plain.append(name)
                continue
            trackers.append(t)
        keep[id(K)] = K
        info[id(K)] = {"id": kid, "tracked": True, "trackers": trackers, "plain": plain}

    def bases_ok(K):
        for b in K.__mro__[1:]:
            if b is object or getattr(b, "__module__", "") == "typing":
                continue
            i = info.get(id(b))
            if i is None or not i["tracked"]:
                return False
        return True

    def register(K, reads):
        kid = f"{K.__module__}:{K.__qualname__}"
        dup = known_ids.get(kid, 0)
        known_ids[kid] = dup + 1
        ok = (
            not dup
            and kid not in bad
            and "<locals>" not in kid
            and (type(K) is type or type(K).__name__ == "_Dialect")
            and bases_ok(K)
        )
        if dup:
            kid = f"{kid}#{dup + 1}"
        if ok:
            # classes the metaclass made with type(...) for this class (e.g. a dialect's
            # default Tokenizer): their derived attributes live in their own __dict__
            for name, v in list(K.__dict__.items()):
                if (
                    isinstance(v, type)
                    and id(v) not in info
                    and getattr(v, "__module__", "").startswith("sqlglot")
                    and type(v) is type
                    and bases_ok(v)
                ):
                    wrap(v, f"{kid}.{name}~")
            wrap(K, kid)
        else:
            keep[id(K)] = K
            info[id(K)] = {"id": kid, "tracked": False, "trackers": []}
        if reads:
            edges[id(K)] = reads

    def bc(func, name, *bases, **kw):
        f = func.__globals__.get("__file__") or ""
        if not f.startswith(src):
            return orig_bc(func, name, *bases, **kw)
        reads = set()
        ctx.append(reads)
        try:
            K = orig_bc(func, name, *bases, **kw)
        finally:
            ctx.pop()
        try:
            register(K, reads)
        except Exception as e:  # never break the program under test
            sys.stderr.write(f"argus classes: register failed for {name}: {e!r}\n")
        return K

    def imp(name, globals=None, locals=None, fromlist=(), level=0):
        # module-level code run by an import inside a class body is not that class's creation
        if ctx and ctx[-1] is not None:
            ctx.append(None)
            try:
                return orig_imp(name, globals, locals, fromlist, level)
            finally:
                ctx.pop()
        return orig_imp(name, globals, locals, fromlist, level)

    builtins.__build_class__ = bc
    builtins.__import__ = imp

    # Lambdas under sqlglot/ that this process enters. A one-line lambda shares its
    # line with import-time code, so line coverage cannot tell "defined" from
    # "called"; the code object can. Each code object reports once, then is disabled.
    lambdas = set()
    try:
        mon = sys.monitoring
        tool = 4
        mon.use_tool_id(tool, "argus-classes")

        def on_start(code, offset):
            if code.co_name == "<lambda>" and code.co_filename.startswith(src):
                lambdas.add((code.co_filename, code.co_firstlineno))
            return mon.DISABLE

        mon.register_callback(tool, mon.events.PY_START, on_start)
        mon.set_events(tool, mon.events.PY_START)
        have_mon = 1
    except Exception as e:
        sys.stderr.write(f"argus classes: no sys.monitoring: {e!r}\n")
        have_mon = 0

    def rel(p):
        return p[len(root) + 1:] if p.startswith(root + os.sep) else p

    def dump():
        builtins.__build_class__ = orig_bc
        builtins.__import__ = orig_imp
        ctx.append(None)  # anything read from here on is ours, not the test's
        cls_idx, classes = {}, []

        def cid(c):
            i = cls_idx.get(id(c))
            if i is None:
                i = cls_idx[id(c)] = len(classes)
                inf = info.get(id(c))
                ent = {
                    "id": inf["id"] if inf else f"{getattr(c, '__module__', '?')}:{getattr(c, '__qualname__', '?')}",
                    "t": 1 if inf and inf["tracked"] else 0,
                    "k": 1 if inf else 0,
                }
                classes.append(ent)
                ent["mro"] = [cid(b) for b in type.mro(c)[1:] if b is not object]
                if inf and inf["tracked"]:
                    ent["names"] = sorted(t.n for t in inf["trackers"])
                    ent["fn"] = sorted(t.n for t in inf["trackers"] if t.g is not None)
                    ent["un"] = sorted(inf["plain"])
            return i

        reads, over = [], []
        for kid_, inf in list(info.items()):
            K = keep[kid_]
            ki = cid(K)
            d = K.__dict__
            for t in inf["trackers"]:
                if d.get(t.n) is not t:
                    over.append([ki, t.n])
                for o in list(t.s.values()):
                    reads.append([cid(o), t.n, ki])
        eds = []
        for kid_, rs in edges.items():
            ki = cid(keep[kid_])
            for (o, name, k, fn, qn) in rs:
                eds.append([ki, cid(keep[o]), name, cid(keep[k]), rel(fn), qn])
        out = {
            "classes": classes, "reads": reads, "edges": eds, "over": over, "nofunc": int(nofunc),
            "mon": have_mon, "lam": sorted([rel(f), l] for f, l in lambdas),
        }
        try:
            data = zlib.compress(json.dumps(out, separators=(",", ":")).encode(), 6)
            tmp = os.path.join(_dir, f".{_n}.{os.getpid()}.tmp")
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, os.path.join(_dir, f"{_n}.{os.getpid()}.json.z"))
        except Exception as e:
            sys.stderr.write(f"argus classes: dump failed: {e!r}\n")

    atexit.register(dump)
    # a process that starts but never writes its record (killed worker) is detected by this marker
    try:
        open(os.path.join(_dir, f"{_n}.{os.getpid()}.start"), "w").close()
    except Exception:
        pass


if _dir and _n:
    _install()
