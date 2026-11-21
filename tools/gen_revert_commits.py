"""Realistic validation patches (local only): the reverse of recent real sqlglot commits
before the pinned one, restricted to files that exist at the pinned commit, kept when
they apply cleanly to it.
Usage: python3 tools/gen_revert_commits.py SQLGLOT_GIT OUT COUNT [MAX_FILES]
"""
import os
import subprocess
import sys

repo, out, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
max_files = int(sys.argv[4]) if len(sys.argv) > 4 else 8
os.makedirs(os.path.join(out, "commits"), exist_ok=True)


def git(*args, check=True, input=None):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=check, input=input)


commits = git("rev-list", "--no-merges", "--max-count", str(count * 4), "HEAD~1").stdout.split()
made = 0
for c in commits:
    if made >= count:
        break
    files = [f for f in git("diff-tree", "--no-commit-id", "--name-only", "-r", c).stdout.split() if f]
    if not files or len(files) > max_files:
        continue
    if not any(f.startswith("sqlglot/") and f.endswith(".py") for f in files):
        continue
    patch = git("diff", "--no-color", "-R", f"{c}~1", c, "--", *files, check=False).stdout
    if not patch.strip() or "Binary files" in patch:
        continue
    if git("apply", "--check", "-", check=False, input=patch).returncode != 0:
        continue
    cid = "r%03d" % made
    with open(os.path.join(out, "commits", cid + ".patch"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(patch)
    with open(os.path.join(out, "sources.txt"), "a") as fh:
        fh.write(f"{cid} {c} {len(files)} files\n")
    made += 1
print("wrote", made, "of", len(commits), "candidates")
