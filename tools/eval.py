"""Local evaluation: runs selector.py on a commit set and prints the cost of each commit.

Usage (WSL): python3 tools/eval.py [sample_dir] [--quiet]
Env: ARGUS_SQLGLOT (pinned sqlglot checkout), ARGUS_MAP (map/cov.sqlite).
Scoring follows SCORING.md; the CPU time printed is this process tree's, a rough
stand-in for the container's.
"""
import json
import os
import resource
import subprocess
import sys

here = os.path.dirname(os.path.abspath(__file__))
repo = os.path.dirname(here)
args = [a for a in sys.argv[1:] if not a.startswith("--")]
quiet = "--quiet" in sys.argv
sample = args[0] if args else os.path.join(repo, "sample")
env = dict(os.environ, ARGUS_DATA=sample)
env.setdefault("ARGUS_SQLGLOT", "/root/argus-work/sqlglot")
env.setdefault("ARGUS_MAP", os.path.join(repo, "map", "cov.sqlite"))
r = subprocess.run([sys.executable, os.path.join(repo, "selector.py")], env=env,
                   capture_output=True, text=True)
ru = resource.getrusage(resource.RUSAGE_CHILDREN)
if r.returncode:
    print(r.stderr[-3000:])
    sys.exit(1)
why = {}
for line in r.stderr.splitlines():
    cid, _, rest = line.partition(" ")
    why[cid] = rest

tests = json.load(open(os.path.join(sample, "tests.json")))["tests"]
dur = {t["number"]: round(t["seconds"] * 1000) for t in tests}
answers = json.load(open(os.path.join(sample, "answers.json")))["commits"]


def best_cost(ms):
    if not ms:
        return 0
    ms = sorted(ms, reverse=True)
    best = None
    for w in range(1, 9):
        loads = [0] * w
        for d in ms:
            loads[loads.index(min(loads))] += d
        c = max(loads) + 2000 * w
        best = c if best is None else min(best, c)
    return best


total = oracle = misses = 0
rows = []
for line in r.stdout.splitlines():
    o = json.loads(line)
    cid, workers = o["commit"], [w for w in o["workers"] if w]
    sel = {t for w in workers for t in w}
    cost = (max(sum(dur[t] for t in w) for w in workers) + 2000 * len(workers)) if workers else 0
    fail = set(answers[cid]["failing"])
    miss = bool(fail - sel)
    misses += miss
    total += cost + 300000 * miss
    ob = best_cost([dur[t] for t in fail])
    oracle += ob
    rows.append((cost + 300000 * miss - ob, cid, answers[cid]["kind"], len(sel), len(fail), cost, ob, miss, why.get(cid, "")))
if not quiet:
    for gap, cid, kind, ns, nf, cost, ob, miss, w in sorted(rows, reverse=True):
        flag = "  MISS" if miss else ""
        print(f"{cid} {kind:8} sel={ns:5d} fail={nf:3d} cost={cost/1000:7.1f}s oracle={ob/1000:6.1f}s gap={gap/1000:7.1f}{flag} | {w[:110]}")
print(f"SIMULATED {total/1000:.1f}s  misses={misses}  oracle={oracle/1000:.1f}s  "
      f"selector CPU={ru.ru_utime + ru.ru_stime:.2f}s")
