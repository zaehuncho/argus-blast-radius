"""Generates sample-style patches against the pinned sqlglot checkout, for local
validation only (ground truth comes from running the full suite; nothing from it
ships in the selector).

Mix modelled on the official 30 samples: about 53% one-line code mutations
(and/or, comparison operators, is/==, not, True/False, integer +-1), 27% one-line
fixture corruptions, 10% inserted comments, 10% multi-file combinations.

Usage: python3 tools/gen_sample_style.py SQLGLOT_DIR OUT_DIR COUNT [SEED]
"""
import ast
import difflib
import glob
import json
import os
import random
import re
import sys

src, out, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
rng = random.Random(int(sys.argv[4]) if len(sys.argv) > 4 else 20261121)
os.makedirs(os.path.join(out, "commits"), exist_ok=True)

PY = sorted(p for p in glob.glob(os.path.join(src, "sqlglot", "**", "*.py"), recursive=True) if "__pycache__" not in p)
FIX = sorted(glob.glob(os.path.join(src, "tests", "fixtures", "optimizer", "*.sql")) +
             glob.glob(os.path.join(src, "tests", "fixtures", "*.sql")))
COMMENTS = ["# Cheap check first", "# Keep in sync with the generator", "# Kept for backwards compatibility",
            "# Mirrors the behavior of the reference implementation", "# Intentionally not cached",
            "# Matches upstream semantics", "# See the dialect docs"]

SWAPS = [(" and ", " or "), (" or ", " and "), (" == ", " != "), (" != ", " == "), (" < ", " <= "),
         (" <= ", " < "), (" > ", " >= "), (" >= ", " > "), (" is not ", " != "), (" is ", " == "),
         (" not in ", " in "), ("True", "False"), ("False", "True"), ("if not ", "if ")]


def rel(p):
    return os.path.relpath(p, src).replace(os.sep, "/")


def diff(path, old, new):
    a, b = old.split("\n"), new.split("\n")
    body = list(difflib.unified_diff(a, b, f"a/{path}", f"b/{path}", lineterm="", n=3))
    if not body:
        return None
    return f"diff --git a/{path} b/{path}\nindex 0000000..1111111 100644\n" + "\n".join(body) + "\n"


def code_mutation():
    for _ in range(200):
        p = rng.choice(PY)
        text = open(p, encoding="utf-8").read()
        lines = text.split("\n")
        i = rng.randrange(len(lines))
        line = lines[i]
        s = line.strip()
        if not s or s.startswith(("#", "def ", "class ", "@", "import ", "from ")) or '"""' in s:
            continue
        options = [(a, b) for a, b in SWAPS if a in line]
        nums = list(re.finditer(r"(?<![\w.])(\d+)(?![\w.])", line))
        if nums:
            options.append(("INT", None))
        if not options:
            continue
        a, b = rng.choice(options)
        if a == "INT":
            m = rng.choice(nums)
            n = int(m.group(1))
            new_line = line[:m.start(1)] + str(n + rng.choice([1, -1]) if n > 0 else n + 1) + line[m.end(1):]
        else:
            k = line.find(a)
            new_line = line[:k] + b + line[k + len(a):]
        if new_line == line:
            continue
        new = "\n".join(lines[:i] + [new_line] + lines[i + 1:])
        try:
            ast.parse(new)
        except SyntaxError:
            continue
        return rel(p), text, new, "code"
    return None


def comment_mutation():
    for _ in range(200):
        p = rng.choice(PY)
        text = open(p, encoding="utf-8").read()
        lines = text.split("\n")
        i = rng.randrange(1, len(lines))
        s = lines[i].strip()
        if not s or s.startswith(("#", "@", ")", "]", "}")) or not lines[i - 1].rstrip().endswith((":", ")", "]", "}", '"', "'")) and not lines[i - 1].strip() == "":
            continue
        indent = lines[i][:len(lines[i]) - len(lines[i].lstrip())]
        new = "\n".join(lines[:i] + [indent + rng.choice(COMMENTS)] + lines[i:])
        try:
            ast.parse(new)
        except SyntaxError:
            continue
        return rel(p), text, new, "comment"
    return None


def fixture_mutation():
    for _ in range(200):
        p = rng.choice(FIX)
        text = open(p, encoding="utf-8").read()
        lines = text.split("\n")
        i = rng.randrange(len(lines))
        s = lines[i]
        if not s.strip() or s.lstrip().startswith(("#", "--")):
            continue
        spaces = [m.start() for m in re.finditer(" ", s)]
        if rng.random() < 0.5 and spaces:
            k = rng.choice(spaces)
            new_line = s[:k] + " x" + s[k:]
        else:
            k = rng.randrange(len(s))
            new_line = s[:k] + s[k + 1:]
        if new_line == s:
            continue
        return rel(p), text, "\n".join(lines[:i] + [new_line] + lines[i + 1:]), "fixture"
    return None


made, meta = 0, {}
while made < count:
    r = rng.random()
    if r < 0.53:
        parts = [code_mutation()]
        kind = "code"
    elif r < 0.80:
        parts = [fixture_mutation()]
        kind = "fixture"
    elif r < 0.90:
        parts = [comment_mutation()]
        kind = "comment"
    else:
        parts = [rng.choice([code_mutation, comment_mutation])(), rng.choice([comment_mutation, code_mutation])(),
                 fixture_mutation()]
        kind = "multi"
    parts = [x for x in parts if x]
    paths = [x[0] for x in parts]
    if not parts or len(set(paths)) != len(paths):
        continue
    text = "".join(d for d in (diff(p, o, n) for p, o, n, _ in parts) if d)
    if not text:
        continue
    cid = "s%03d" % made
    open(os.path.join(out, "commits", cid + ".patch"), "w", encoding="utf-8", newline="\n").write(text)
    meta[cid] = {"kind": kind, "files": paths}
    made += 1
json.dump(meta, open(os.path.join(out, "meta.json"), "w"), indent=1)
print("wrote", made, "patches;", {k: sum(1 for v in meta.values() if v["kind"] == k) for k in ("code", "fixture", "comment", "multi")})
