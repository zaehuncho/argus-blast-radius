"""Local evaluation of class-level rules against selector.py as it is.

Modes compared (each replaces 'every test that loads the module' where it can):
  base        selector.py unchanged
  class       classmap.narrow(): recorded attribute reads (the proposed rule)
  site-usage  attribute-read-site rule: tests whose coverage runs a line that mentions
              the attribute, intersected with the class's users; if a mention runs at
              import or class creation, the class's users alone
  site-broad  the same, but an import-time mention keeps the broad rule
  both        class, intersected with site-broad where both answer

Usage (WSL): python3 tools/classmap_eval.py [sample_dir] [--quiet] [--only-changed] [--modes=a,b]
Env: ARGUS_SQLGLOT (pinned checkout), ARGUS_MAP (map/cov.sqlite), ARGUS_CLASSMAP (map/classes.sqlite).
Scoring follows SCORING.md. selector.py is imported, not edited: analyse_python
below is a copy of the selector's with the marked lines changed, which is also
the wiring to apply for mode 'class'.
"""
import ast
import json
import os
import sys
import time

here = os.path.dirname(os.path.abspath(__file__))
repo = os.path.dirname(here)
args = [a for a in sys.argv[1:] if not a.startswith("--")]
quiet = "--quiet" in sys.argv
only_changed = "--only-changed" in sys.argv
modes = ["base", "class", "site-usage", "site-broad", "both"]
for a in sys.argv[1:]:
    if a.startswith("--modes="):
        modes = a.split("=", 1)[1].split(",")
sample = args[0] if args else os.path.join(repo, "sample")
os.environ.setdefault("ARGUS_SQLGLOT", "/root/argus-work/sqlglot")
os.environ.setdefault("ARGUS_MAP", os.path.join(repo, "map", "cov.sqlite"))
os.environ["ARGUS_DATA"] = sample
sys.path.insert(0, repo)
import classmap  # noqa: E402
import selector as S  # noqa: E402

SRC = os.environ["ARGUS_SQLGLOT"]
CM = classmap.ClassMap(os.environ.get("ARGUS_CLASSMAP", os.path.join(repo, "map", "classes.sqlite")))
MODE = "base"
HOOK_FUNCS = {"__new__", "__init_subclass__", "__set_name__", "__prepare__"}


class ReadSites:
    """Every place in sqlglot/ and tests/ that mentions an attribute name: x.NAME,
    a bare NAME in a class body, or the string "NAME" (getattr and friends).
    Each site is (path, first line, last line of its statement, runs at import)."""

    def __init__(self):
        self.sites = {}
        for top in ("sqlglot", "tests"):
            for d, _, fs in os.walk(os.path.join(SRC, top)):
                for f in fs:
                    if not f.endswith(".py"):
                        continue
                    full = os.path.join(d, f)
                    path = os.path.relpath(full, SRC).replace(os.sep, "/")
                    try:
                        tree = ast.parse(open(full, encoding="utf-8").read())
                    except SyntaxError:
                        continue
                    for st in tree.body:
                        self._stmt(st, path, True, False)

    def _stmt(self, st, path, at_import, in_class):
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # decorators and defaults run where the def runs; the body runs when called,
            # except creation hooks, which run while classes are built
            for x in st.decorator_list + st.args.defaults + [k for k in st.args.kw_defaults if k]:
                self._expr(x, path, st, at_import, in_class)
            hook = st.name in HOOK_FUNCS
            for x in st.body:
                self._stmt(x, path, hook, False)
            return
        if isinstance(st, ast.ClassDef):
            for x in st.decorator_list + st.bases + [k.value for k in st.keywords]:
                self._expr(x, path, st, at_import, in_class)
            for x in st.body:
                self._stmt(x, path, at_import, True)
            return
        for child in ast.iter_child_nodes(st):
            if isinstance(child, ast.stmt):
                self._stmt(child, path, at_import, in_class)
            elif isinstance(child, (ast.excepthandler, getattr(ast, "match_case", ast.excepthandler))):
                for x in ast.iter_child_nodes(child):
                    if isinstance(x, ast.stmt):
                        self._stmt(x, path, at_import, in_class)
                    else:
                        self._expr(x, path, st, at_import, in_class)
            else:
                self._expr(child, path, st, at_import, in_class)

    def _expr(self, n, path, st, at_import, in_class):
        if isinstance(n, ast.Lambda):
            for x in n.args.defaults + [k for k in n.args.kw_defaults if k]:
                self._expr(x, path, st, at_import, in_class)
            self._expr(n.body, path, st, False, False)
            return
        name = None
        if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load):
            name = n.attr
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.isidentifier():
            name = n.value
        elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and in_class:
            name = n.id
        if name is not None:
            self.sites.setdefault(name, []).append((path, st.lineno, st.end_lineno, at_import))
        for child in ast.iter_child_nodes(n):
            self._expr(child, path, st, at_import, in_class)


_sites = None


def site_rule(path, old_text, new_text, old_tree, new_tree, cov, import_fallback):
    """The attribute-read-site rule. Returns (tests, reason) or None for the broad rule."""
    global _sites
    try:
        edits, funcs, lams, _ = CM.diff(path, old_text, new_text, old_tree, new_tree)
    except classmap.Broad:
        return None
    if lams or any(e[1] != "attr" for e in edits):
        return None  # the rule speaks only about NAME = ... assignments
    if _sites is None:
        _sites = ReadSites()
    tests = set()
    for lo, hi in funcs:
        tests |= cov.tests_covering(path, range(lo, hi + 1))
    notes = []
    for cname, kind, name, sa, sb, ca, cb in edits:
        if not (cname in CM.cls and CM.cls[cname]["tracked"]):
            return None
        users = CM._users(cname) | CM.always
        sites = _sites.sites.get(name, [])
        if any(s[3] for s in sites):
            if import_fallback == "broad":
                return None
            tests |= users
            notes.append(f"{name}: import-time read, {len(users)} users")
            continue
        got = set()
        for p, lo, hi, _ in sites:
            got |= cov.tests_covering(p, range(lo, hi + 1))
        tests |= got & users
        notes.append(f"{name}: {len(sites)} sites, {len(got & users)} of {len(users)} users")
    return tests, "; ".join(notes) or "function bodies only"


def analyse_python(path, old_text, hunks, cov):
    new_text, changed = S.apply_hunks(old_text, hunks)
    try:
        old_tree = ast.parse(old_text)
    except SyntaxError:
        return None, "old file does not parse"
    try:
        new_tree = ast.parse(new_text)
    except SyntaxError:
        return None, "patched file does not parse"
    if S.same_tree(old_tree, new_tree):
        return set(), "no code change"
    if S.equivalent_tree(old_tree, new_tree):
        return set(), "equivalent None comparison"
    tests = set()
    module_level = False
    for l in sorted(changed):
        stmt, in_func = S.locate(old_tree, l)
        if stmt is None:
            module_level = True
            continue
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and l <= S.header_end(stmt):
            if in_func:
                tests |= cov.tests_covering(path, range(S.first_line(stmt), stmt.end_lineno + 1))
            else:
                module_level = True
            continue
        if not in_func:
            lam = S.lambda_body_at(stmt, l)
            if lam is not None:
                tests |= cov.tests_covering(path, [l])
                if MODE in ("class", "both"):
                    # the wiring, part 2: a changed line of a multi-line lambda may be one
                    # that short-circuiting skipped; take every test that called the lambda
                    tests |= CM.entered(path, lam.lineno)
            else:
                module_level = True
            continue
        lo = S.first_line(stmt)
        hi = S.header_end(stmt) if l <= S.header_end(stmt) else stmt.end_lineno
        tests |= cov.tests_covering(path, range(lo, hi + 1))
    if module_level:
        narrowed = None
        if MODE in ("class", "both"):
            narrowed = classmap.narrow(CM, cov, path, old_text, new_text, old_tree, new_tree)  # the wiring
        if MODE in ("site-usage", "site-broad", "both"):
            other = site_rule(path, old_text, new_text, old_tree, new_tree, cov,
                              "users" if MODE == "site-usage" else "broad")
            if MODE == "both":
                if narrowed is not None and other is not None:
                    narrowed = (narrowed[0] & other[0], narrowed[1] + " & sites")
            else:
                narrowed = other
        if narrowed is not None:                                                         # the wiring
            return tests | narrowed[0], f"{MODE}: " + narrowed[1]                        # the wiring
        tests |= cov.tests_touching_file(path)
    return tests, "module level" if module_level else "function body"


S.analyse_python = analyse_python

tests = json.load(open(os.path.join(sample, "tests.json")))["tests"]
dur = {t["number"]: round(t["seconds"] * 1000) for t in tests}
every = set(dur)
nodeid_num = {t["id"]: t["number"] for t in tests}
tests_by_file = {}
for t in tests:
    tests_by_file.setdefault(t["file"], []).append(t["number"])
apath = os.path.join(sample, "answers.json")
answers = json.load(open(apath))["commits"] if os.path.exists(apath) else None
cov = S.Map(os.environ["ARGUS_MAP"])


def cost_of(selected):
    workers = [w for w in S.pack(selected, dur) if w]
    return (max(sum(dur[t] for t in w) for w in workers) + S.WORKER_COST_MS * len(workers)) if workers else 0


def run(mode):
    global MODE
    MODE = mode
    res = {}
    cdir = os.path.join(sample, "commits")
    for name in sorted(os.listdir(cdir)):
        if not name.endswith(".patch"):
            continue
        reasons = []
        try:
            text = open(os.path.join(cdir, name), encoding="utf-8", errors="replace").read()
            sel = S.select(text, every, cov, tests_by_file, nodeid_num, reasons) & every
        except Exception as e:
            sel = set(every)
            reasons.append(f"{type(e).__name__} {e} -> all")
        res[name[:-6]] = (sel, "; ".join(reasons))
    return res


results, cpu = {}, {}
for m in modes:
    t0 = time.process_time()
    results[m] = run(m)
    cpu[m] = time.process_time() - t0
base = results[modes[0]]
if not quiet:
    for cid in sorted(base):
        fail = set(answers[cid]["failing"]) if answers and cid in answers else None
        sels = [results[m][cid][0] for m in modes]
        if only_changed and all(s == sels[0] for s in sels):
            continue
        cells = []
        for m, s in zip(modes, sels):
            miss = " MISS" if fail is not None and fail - s else ""
            cells.append(f"{m} {len(s)}/{cost_of(s) / 1000:.1f}s{miss}")
        nf = len(fail) if fail is not None else -1
        print(f"{cid} fail={nf:3d} | " + " | ".join(cells))
        for m in modes[1:]:
            if results[m][cid][0] != sels[0]:
                print(f"      {results[m][cid][1][:170]}")
print(f"commits={len(base)} answers={'yes' if answers else 'NO'}")
for m in modes:
    total = misses = 0
    missed = []
    for cid, (sel, _) in results[m].items():
        fail = set(answers[cid]["failing"]) if answers and cid in answers else set()
        miss = bool(fail - sel)
        misses += miss
        if miss:
            missed.append(cid)
        total += cost_of(sel) + 300000 * miss
    changed = sum(results[m][c][0] != base[c][0] for c in base)
    print(f"{m:11} simulated {total / 1000:8.1f}s  misses={misses} {missed if missed else ''}  "
          f"changed={changed}  selector CPU={cpu[m]:.2f}s")
