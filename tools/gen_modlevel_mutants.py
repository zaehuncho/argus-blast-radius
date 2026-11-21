"""Sample-style one-line swaps on lines of top-level module assignments (NAME = value)
in sqlglot/, to validate module-constant narrowing (local only). File first, then a line.
Usage: python3 tools/gen_modlevel_mutants.py SQLGLOT OUT COUNT [SEED]
"""
import ast
import difflib
import glob
import os
import random
import re
import sys

src, out, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
rng = random.Random(int(sys.argv[4]) if len(sys.argv) > 4 else 21)
os.makedirs(os.path.join(out, "commits"), exist_ok=True)
SWAPS = [(" and ", " or "), (" or ", " and "), (" == ", " != "), (" != ", " == "), (" < ", " <= "),
         (" <= ", " < "), (" > ", " >= "), (" >= ", " > "), (" not in ", " in "), (" in ", " not in "),
         ("True", "False"), ("False", "True"), (" is not ", " is "), (" is ", " is not ")]

sites = {}
for p in sorted(glob.glob(os.path.join(src, "sqlglot", "**", "*.py"), recursive=True)):
    rel = os.path.relpath(p, src)
    tree = ast.parse(open(p, encoding="utf-8").read())
    for st in tree.body:
        if isinstance(st, (ast.Assign, ast.AnnAssign)) and st.value is not None:
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
    if line.strip().startswith("#"):
        continue
    opts = [(a, b) for a, b in SWAPS if a in line]
    nums = list(re.finditer(r"(?<![\w.])(\d+)(?![\w.])", line))
    if nums:
        opts.append(("INT", None))
    if not opts:
        continue
    a, b = rng.choice(opts)
    if a == "INT":
        m = rng.choice(nums)
        n = int(m.group(1))
        new_line = line[:m.start(1)] + str(n + 1 if n == 0 else n + rng.choice([1, -1])) + line[m.end(1):]
    else:
        k = line.find(a)
        new_line = line[:k] + b + line[k + len(a):]
    new = "\n".join(lines[:l - 1] + [new_line] + lines[l:])
    try:
        ast.parse(new)
    except SyntaxError:
        continue
    body = "\n".join(difflib.unified_diff(lines, new.split("\n"), f"a/{rel}", f"b/{rel}", lineterm="", n=3))
    with open(os.path.join(out, "commits", "m%03d.patch" % made), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f"diff --git a/{rel} b/{rel}\nindex 0000000..1111111 100644\n{body}\n")
    made += 1
print(f"{sum(len(v) for v in sites.values())} lines in {len(sites)} files; wrote {made}")
