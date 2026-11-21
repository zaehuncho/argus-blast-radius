"""Makes class-body mutants that the class-map rule narrows, to check its recall.

Each mutant changes one constant inside a class-level assignment (flag flip,
number +1, string edit), removes one single-line dict/set entry, or changes a
constant default in a method signature. Only mutants for which
classmap.ClassMap.analyse() answers (i.e. the rule would replace the broad
"every test that loads the module" set) are kept, spread over classes.

Usage (WSL): python3 tools/classmap_mutants.py OUT_DIR [count] [seed]
Writes OUT_DIR/commits/<id>.patch, OUT_DIR/tests.json (copy) and OUT_DIR/meta.json.
Ground truth then comes from tools/mutants/harness.py (full suite per patch).
"""
import ast
import difflib
import json
import os
import random
import shutil
import sys

here = os.path.dirname(os.path.abspath(__file__))
repo = os.path.dirname(here)
sys.path.insert(0, repo)
import classmap  # noqa: E402

out = sys.argv[1]
count = int(sys.argv[2]) if len(sys.argv) > 2 else 48
seed = int(sys.argv[3]) if len(sys.argv) > 3 else 7
src = os.environ.get("ARGUS_SQLGLOT", "/root/argus-work/sqlglot")
cm = classmap.ClassMap(os.environ.get("ARGUS_CLASSMAP", os.path.join(repo, "map", "classes.sqlite")))
rng = random.Random(seed)


def replace(lines, node, text):
    """Source with `node` (single-line) replaced by `text`."""
    l = lines[node.lineno - 1]
    b = l.encode("utf-8")
    new = b[: node.col_offset] + text.encode("utf-8") + b[node.end_col_offset:]
    res = list(lines)
    res[node.lineno - 1] = new.decode("utf-8")
    return res


def const_mutations(lines, value):
    """(description, new lines) for constants evaluated at import inside `value`."""
    for n in classmap._import_level(value):
        if not isinstance(n, ast.Constant) or n.lineno != n.end_lineno:
            continue
        v = n.value
        if isinstance(v, bool):
            yield f"bool flip {v}", replace(lines, n, str(not v))
        elif isinstance(v, int):
            yield f"int {v}+1", replace(lines, n, str(v + 1))
        elif isinstance(v, str) and v and v.isascii() and "\\" not in v and "'" not in v and '"' not in v:
            seg = lines[n.lineno - 1].encode()[n.col_offset:n.end_col_offset].decode()
            if seg[:1] in "'\"" and seg[1:-1] == v:
                w = v[:-1] + ("Q" if v[-1] != "Q" else "Z")
                yield f"str {v!r}->{w!r}", replace(lines, n, seg[0] + w + seg[0])


def entry_removals(lines, value):
    """Remove one dict or set entry that sits alone on its own line."""
    if isinstance(value, ast.Dict):
        ents = [(k, v) for k, v in zip(value.keys, value.values) if k is not None]
        spans = [(k.lineno, v.end_lineno) for k, v in ents]
    elif isinstance(value, ast.Set):
        spans = [(e.lineno, e.end_lineno) for e in value.elts]
    else:
        return
    used = {}
    for lo, hi in spans:
        for l in range(lo, hi + 1):
            used[l] = used.get(l, 0) + 1
    for lo, hi in spans:
        if lo == hi and used[lo] == 1 and lo not in (value.lineno, value.end_lineno) and lines[lo - 1].rstrip().endswith(","):
            res = list(lines)
            del res[lo - 1]
            yield f"remove entry at line {lo}", res


cands = []
for d, _, fs in os.walk(os.path.join(src, "sqlglot")):
    for f in sorted(fs):
        if not f.endswith(".py"):
            continue
        full = os.path.join(d, f)
        path = os.path.relpath(full, src).replace(os.sep, "/")
        text = open(full, encoding="utf-8").read()
        lines = text.split("\n")
        tree = ast.parse(text)

        def walk(body, qual):
            for s in body:
                if isinstance(s, ast.ClassDef):
                    q = f"{qual}.{s.name}" if qual else s.name
                    if cm.known(path, q):
                        for st in s.body:
                            k = classmap._key(st)
                            if k and k[0] == "attr":
                                for desc, new in const_mutations(lines, st.value):
                                    cands.append((path, q, k[1], "const", desc, new))
                                for desc, new in entry_removals(lines, st.value):
                                    cands.append((path, q, k[1], "remove", desc, new))
                            elif k and k[0] == "def":
                                for dflt in st.args.defaults + [x for x in st.args.kw_defaults if x is not None]:
                                    for desc, new in const_mutations(lines, dflt):
                                        cands.append((path, q, k[1], "default", desc, new))
                    walk(s.body, q)

        walk(tree.body, "")

print("candidates", len(cands), file=sys.stderr)
rng.shuffle(cands)
os.makedirs(os.path.join(out, "commits"), exist_ok=True)
shutil.copy(os.path.join(repo, "sample", "tests.json"), os.path.join(out, "tests.json"))
cov_files = None
meta, per_class, per_kind = {}, {}, {}
texts = {}
tried = 0
for path, q, name, kind, desc, new in cands:
    if len(meta) >= count:
        break
    key = (path, q)
    if per_class.get(key, 0) >= 2 or per_kind.get((kind, path.split("/")[1]), 0) >= max(3, count // 6):
        continue
    if path not in texts:
        texts[path] = open(os.path.join(src, path), encoding="utf-8").read()
    old_text, new_text = texts[path], "\n".join(new)
    tried += 1
    try:
        ast.parse(new_text)
    except SyntaxError:
        continue
    v = cm.analyse(path, old_text, new_text)
    if v.tests is None:
        continue
    cid = "cm%02d" % len(meta)
    diff = "".join(
        difflib.unified_diff(
            old_text.splitlines(keepends=True), new_text.splitlines(keepends=True), f"a/{path}", f"b/{path}", n=3
        )
    )
    with open(os.path.join(out, "commits", cid + ".patch"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f"diff --git a/{path} b/{path}\n" + diff)
    meta[cid] = {"path": path, "class": q, "name": name, "kind": kind, "desc": desc, "selected": len(v.tests), "reason": v.reason}
    per_class[key] = per_class.get(key, 0) + 1
    per_kind[(kind, path.split("/")[1])] = per_kind.get((kind, path.split("/")[1]), 0) + 1
json.dump(meta, open(os.path.join(out, "meta.json"), "w"), indent=1, sort_keys=True)
print(f"kept {len(meta)} of {tried} tried", file=sys.stderr)
