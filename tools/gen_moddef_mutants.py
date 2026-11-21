"""Top-level function edits (local validation only): delete an undecorated def, add a
new helper def, rename a def, or add a defaulted parameter. File first, then a site.
Usage: python3 tools/gen_moddef_mutants.py SQLGLOT OUT COUNT [SEED]
"""
import ast
import difflib
import glob
import os
import random
import sys

src, out, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
rng = random.Random(int(sys.argv[4]) if len(sys.argv) > 4 else 41)
os.makedirs(os.path.join(out, "commits"), exist_ok=True)
files = sorted(glob.glob(os.path.join(src, "sqlglot", "**", "*.py"), recursive=True))
made, tries = 0, 0
while made < count and tries < count * 200:
    tries += 1
    p = rng.choice(files)
    rel = os.path.relpath(p, src)
    text = open(p, encoding="utf-8").read()
    lines = text.split("\n")
    tree = ast.parse(text)
    defs = [s for s in tree.body if isinstance(s, ast.FunctionDef) and not s.decorator_list]
    kind = rng.choice(["delete", "add", "rename", "param"])
    new_lines = None
    if kind == "add":
        at = rng.choice([s for s in tree.body if isinstance(s, (ast.FunctionDef, ast.ClassDef))] or [None])
        if at is None:
            continue
        lo = min([at.lineno] + [d.lineno for d in at.decorator_list])
        helper = [f"def _argus_helper_{made}(value):", "    return value", "", ""]
        new_lines = lines[:lo - 1] + helper + lines[lo - 1:]
    elif defs:
        d = rng.choice(defs)
        if kind == "delete":
            new_lines = lines[:d.lineno - 1] + lines[d.end_lineno:]
        elif kind == "rename":
            head = lines[d.lineno - 1]
            new_head = head.replace(f"def {d.name}(", f"def {d.name}_renamed(", 1)
            if new_head == head:
                continue
            new_lines = lines[:d.lineno - 1] + [new_head] + lines[d.lineno:]
        elif kind == "param":
            head = lines[d.lineno - 1]
            k = head.find("(")
            if k < 0 or not head.rstrip().endswith(":") or ")" not in head:
                continue
            close = head.rfind(")")
            inner = head[k + 1:close].strip()
            if not inner or inner.endswith(",") or "*" in inner or "/" in inner:
                continue
            new_head = head[:close] + ", argus_extra=None" + head[close:]
            new_lines = lines[:d.lineno - 1] + [new_head] + lines[d.lineno:]
    if new_lines is None or new_lines == lines:
        continue
    new = "\n".join(new_lines)
    try:
        ast.parse(new)
    except SyntaxError:
        continue
    body = "\n".join(difflib.unified_diff(lines, new_lines, f"a/{rel}", f"b/{rel}", lineterm="", n=3))
    with open(os.path.join(out, "commits", "f%03d.patch" % made), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f"diff --git a/{rel} b/{rel}\nindex 0000000..1111111 100644\n{body}\n")
    made += 1
print("wrote", made)
