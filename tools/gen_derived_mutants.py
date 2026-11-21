"""Sample-style swaps on lines of class attributes that another class attribute is
computed from in the same class body (e.g. SHOW_PARSERS -> SHOW_TRIE), to validate the
class map's derived-attribute rule (local only).
Usage: python3 tools/gen_derived_mutants.py SQLGLOT OUT COUNT [SEED]
"""
import ast
import difflib
import glob
import os
import random
import re
import sys

src, out, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
rng = random.Random(int(sys.argv[4]) if len(sys.argv) > 4 else 13)
os.makedirs(os.path.join(out, "commits"), exist_ok=True)
SWAPS = [(" and ", " or "), (" or ", " and "), (" == ", " != "), (" != ", " == "), (" not in ", " in "),
         ("True", "False"), ("False", "True")]

sites = {}
for p in sorted(glob.glob(os.path.join(src, "sqlglot", "**", "*.py"), recursive=True)):
    rel = os.path.relpath(p, src)
    tree = ast.parse(open(p, encoding="utf-8").read())
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        attrs = {}
        for st in cls.body:
            if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
                attrs[st.targets[0].id] = st
            elif isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name) and st.value is not None:
                attrs[st.target.id] = st
        for name, st in attrs.items():
            used = any(isinstance(n, ast.Name) and n.id == name
                       for other_name, other in attrs.items() if other_name != name
                       for n in ast.walk(other.value))
            if used:
                sites.setdefault(rel, []).extend(range(st.value.lineno, st.value.end_lineno + 1))

made, tries = 0, 0
files = sorted(sites)
while made < count and tries < count * 400:
    tries += 1
    rel = rng.choice(files)
    l = rng.choice(sites[rel])
    text = open(os.path.join(src, rel), encoding="utf-8").read()
    lines = text.split("\n")
    line = lines[l - 1]
    if line.strip().startswith("#") or "lambda" in line:
        continue
    opts = [(a, b) for a, b in SWAPS if a in line]
    strs = list(re.finditer(r'"([A-Z][A-Z_ ]+)"', line))
    if strs:
        opts.append(("STR", None))
    if not opts:
        continue
    a, b = rng.choice(opts)
    if a == "STR":
        m = rng.choice(strs)
        new_line = line[:m.end(1)] + "X" + line[m.end(1):]
    else:
        k = line.find(a)
        new_line = line[:k] + b + line[k + len(a):]
    new = "\n".join(lines[:l - 1] + [new_line] + lines[l:])
    try:
        ast.parse(new)
    except SyntaxError:
        continue
    body = "\n".join(difflib.unified_diff(lines, new.split("\n"), f"a/{rel}", f"b/{rel}", lineterm="", n=3))
    with open(os.path.join(out, "commits", "d%03d.patch" % made), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f"diff --git a/{rel} b/{rel}\nindex 0000000..1111111 100644\n{body}\n")
    made += 1
print(f"{sum(len(v) for v in sites.values())} lines in {len(sites)} files; wrote {made}")
