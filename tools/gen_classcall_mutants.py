"""Class-body assignments whose value contains a call: change one constant inside the
value (int +-1, bool flip, string edit), to validate the class map's import-check rule
(local only). Picks a file first, then a site, like the sample commits.
Usage: python3 tools/gen_classcall_mutants.py SQLGLOT OUT COUNT [SEED]
"""
import ast
import difflib
import glob
import os
import random
import sys

src, out, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
rng = random.Random(int(sys.argv[4]) if len(sys.argv) > 4 else 3)
os.makedirs(os.path.join(out, "commits"), exist_ok=True)

sites = {}
for p in sorted(glob.glob(os.path.join(src, "sqlglot", "**", "*.py"), recursive=True)):
    rel = os.path.relpath(p, src)
    text = open(p, encoding="utf-8").read()
    tree = ast.parse(text)
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        for st in cls.body:
            if not isinstance(st, (ast.Assign, ast.AnnAssign)) or st.value is None:
                continue
            if not any(isinstance(n, ast.Call) for n in ast.walk(st.value)):
                continue
            consts = [n for n in ast.walk(st.value) if isinstance(n, ast.Constant)
                      and isinstance(n.value, (int, str, bool)) and n.lineno == n.end_lineno]
            calls = [n for n in ast.walk(st.value) if isinstance(n, ast.Call)]
            # constants that are arguments (or inside arguments) of a call
            inside = {id(c) for call in calls for a in list(call.args) + [k.value for k in call.keywords]
                      for c in ast.walk(a)}
            consts = [c for c in consts if id(c) in inside]
            if consts:
                sites.setdefault(rel, []).extend(consts)

made, tries = 0, 0
files = sorted(sites)
while made < count and tries < count * 50:
    tries += 1
    rel = rng.choice(files)
    c = rng.choice(sites[rel])
    text = open(os.path.join(src, rel), encoding="utf-8").read()
    lines = text.split("\n")
    line = lines[c.lineno - 1]
    seg = line[c.col_offset:c.end_col_offset]
    v = c.value
    if isinstance(v, bool):
        rep = repr(not v)
    elif isinstance(v, int):
        rep = str(v + rng.choice([1, -1])) if v > 0 else str(v + 1)
    else:
        if not v or seg[:1] not in "'\"":
            continue
        rep = seg[0] + (v[:-1] if len(v) > 1 and rng.random() < 0.5 else v + "x") + seg[-1]
    if rep == seg:
        continue
    new_line = line[:c.col_offset] + rep + line[c.end_col_offset:]
    new = "\n".join(lines[:c.lineno - 1] + [new_line] + lines[c.lineno:])
    try:
        ast.parse(new)
    except SyntaxError:
        continue
    body = "\n".join(difflib.unified_diff(lines, new.split("\n"), f"a/{rel}", f"b/{rel}", lineterm="", n=3))
    with open(os.path.join(out, "commits", "k%03d.patch" % made), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f"diff --git a/{rel} b/{rel}\nindex 0000000..1111111 100644\n{body}\n")
    made += 1
print(f"{sum(len(v) for v in sites.values())} sites in {len(sites)} files; wrote {made}")
