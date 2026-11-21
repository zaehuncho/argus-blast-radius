"""Top-level statement insertions and removals (local validation only): delete a
one-line `NAME = value` statement, duplicate one, delete a standard-library import
(or one name from it), or add one. File first, then a site.
Usage: python3 tools/gen_modstmt_mutants.py SQLGLOT OUT COUNT [SEED]
"""
import ast
import difflib
import glob
import os
import random
import sys

src, out, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
rng = random.Random(int(sys.argv[4]) if len(sys.argv) > 4 else 31)
os.makedirs(os.path.join(out, "commits"), exist_ok=True)
STDLIB = set(sys.stdlib_module_names) - {"__future__"}
ADD = ["import os", "import itertools", "from functools import reduce", "import math", "import re"]

files = sorted(glob.glob(os.path.join(src, "sqlglot", "**", "*.py"), recursive=True))
made, tries = 0, 0
while made < count and tries < count * 200:
    tries += 1
    p = rng.choice(files)
    rel = os.path.relpath(p, src)
    text = open(p, encoding="utf-8").read()
    lines = text.split("\n")
    tree = ast.parse(text)
    assigns = [s for s in tree.body if isinstance(s, (ast.Assign, ast.AnnAssign)) and s.lineno == s.end_lineno]
    imports = [s for s in tree.body if isinstance(s, (ast.Import, ast.ImportFrom)) and s.lineno == s.end_lineno
               and ((isinstance(s, ast.Import) and all(a.name.split(".")[0] in STDLIB for a in s.names))
                    or (isinstance(s, ast.ImportFrom) and s.level == 0 and s.module
                        and s.module.split(".")[0] in STDLIB))]
    kind = rng.choice(["del_assign", "dup_assign", "del_import", "add_import"])
    new_lines = None
    if kind == "del_assign" and assigns:
        s = rng.choice(assigns)
        new_lines = lines[:s.lineno - 1] + lines[s.lineno:]
    elif kind == "dup_assign" and assigns:
        s = rng.choice(assigns)
        new_lines = lines[:s.lineno] + [lines[s.lineno - 1]] + lines[s.lineno:]
    elif kind == "del_import" and imports:
        s = rng.choice(imports)
        if len(s.names) > 1 and rng.random() < 0.6:
            keep = [a for a in s.names if a is not rng.choice(s.names)] or s.names[:1]
            names = ", ".join(a.name + (f" as {a.asname}" if a.asname else "") for a in keep)
            head = f"import {names}" if isinstance(s, ast.Import) else f"from {s.module} import {names}"
            new_lines = lines[:s.lineno - 1] + [head] + lines[s.lineno:]
        else:
            new_lines = lines[:s.lineno - 1] + lines[s.lineno:]
    elif kind == "add_import":
        first = next((s for s in tree.body if isinstance(s, (ast.Import, ast.ImportFrom))
                      and not (isinstance(s, ast.ImportFrom) and s.module == "__future__")), None)
        if first is None:
            continue
        new_lines = lines[:first.lineno - 1] + [rng.choice(ADD)] + lines[first.lineno - 1:]
    if new_lines is None or new_lines == lines:
        continue
    new = "\n".join(new_lines)
    try:
        ast.parse(new)
    except SyntaxError:
        continue
    body = "\n".join(difflib.unified_diff(lines, new_lines, f"a/{rel}", f"b/{rel}", lineterm="", n=3))
    with open(os.path.join(out, "commits", "s%03d.patch" % made), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f"diff --git a/{rel} b/{rel}\nindex 0000000..1111111 100644\n{body}\n")
    made += 1
print("wrote", made)
