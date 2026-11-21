"""One-line code mutations on lines test_execute_tpcds covers outside the blocked
lines (map/tpcds_block.json), to validate tpcds_check.py (local only).

Usage (WSL): ARGUS_MAP=map/cov.sqlite ARGUS_TPCDS_BLOCK=... python3 tools/gen_tpcds_mutants.py SQLGLOT OUT COUNT [SEED]
"""
import ast
import difflib
import json
import os
import random
import re
import sys

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(here))
import selector  # noqa: E402

src, out, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
rng = random.Random(int(sys.argv[4]) if len(sys.argv) > 4 else 7)
os.makedirs(os.path.join(out, "commits"), exist_ok=True)
tests = json.load(open(os.path.join(os.path.dirname(here), "sample", "tests.json")))["tests"]
tp = {t["id"]: t["number"] for t in tests}[selector.TPCDS_ID]
cov = selector.Map(selector.MAP)
block = selector.TpcdsBlock(selector.TPCDS_BLOCK)

SWAPS = [(" and ", " or "), (" or ", " and "), (" == ", " != "), (" != ", " == "), (" < ", " <= "),
         (" <= ", " < "), (" > ", " >= "), (" >= ", " > "), (" is not ", " is "), (" is ", " is not "),
         (" not in ", " in "), (" in ", " not in "), ("True", "False"), ("False", "True"), ("if not ", "if "),
         ("return ", "return None and ")]

cands = []
for path in sorted(cov.files):
    if not path.startswith("sqlglot/") or not path.endswith(".py"):
        continue
    bits = cov.lines(path).get(tp)
    if not bits:
        continue
    lines = {i * 8 + b for i, byte in enumerate(bits) for b in range(8) if byte & (1 << b)}
    for l in sorted(lines - block.lines.get(path, set())):
        cands.append((path, l))
print(f"{len(cands)} candidate lines", file=sys.stderr)

made, tries = 0, 0
while made < count and tries < count * 200:
    tries += 1
    path, l = rng.choice(cands)
    text = open(os.path.join(src, path), encoding="utf-8").read()
    lines = text.split("\n")
    line = lines[l - 1]
    s = line.strip()
    if not s or s.startswith(("#", "def ", "class ", "@")):
        continue
    opts = [(a, b) for a, b in SWAPS if a in line and not (a == "return " and not s.startswith("return "))]
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
    body = "\n".join(difflib.unified_diff(text.split("\n"), new.split("\n"), f"a/{path}", f"b/{path}", lineterm="", n=3))
    patch = f"diff --git a/{path} b/{path}\nindex 0000000..1111111 100644\n{body}\n"
    cid = "t%03d" % made
    open(os.path.join(out, "commits", cid + ".patch"), "w", encoding="utf-8", newline="\n").write(patch)
    made += 1
print(f"wrote {made} patches", file=sys.stderr)
