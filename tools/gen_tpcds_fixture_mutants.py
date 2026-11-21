"""One-line edits of tests/fixtures/optimizer/tpc-ds/tpc-ds.sql in the style of the
sample fixture commits (insert " x" at a space, or delete a character), to validate the
TPC-DS check on fixture edits (local only).
Usage: python3 tools/gen_tpcds_fixture_mutants.py SQLGLOT OUT COUNT [SEED]
"""
import difflib
import os
import random
import sys

src, out, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
rng = random.Random(int(sys.argv[4]) if len(sys.argv) > 4 else 5)
path = "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql"
text = open(os.path.join(src, path), encoding="utf-8").read()
lines = text.split("\n")
os.makedirs(os.path.join(out, "commits"), exist_ok=True)
made = 0
while made < count:
    i = rng.randrange(len(lines))
    s = lines[i]
    if not s.strip():
        continue
    if rng.random() < 0.5 and " " in s:
        k = rng.choice([j for j, c in enumerate(s) if c == " "])
        new = s[:k] + " x" + s[k:]
    else:
        k = rng.randrange(len(s))
        new = s[:k] + s[k + 1:]
    if new == s:
        continue
    body = "\n".join(difflib.unified_diff(lines, lines[:i] + [new] + lines[i + 1:], f"a/{path}", f"b/{path}",
                                          lineterm="", n=3))
    with open(os.path.join(out, "commits", "f%03d.patch" % made), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f"diff --git a/{path} b/{path}\nindex 0000000..1111111 100644\n{body}\n")
    made += 1
print("wrote", made)
