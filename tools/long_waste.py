"""For each eval set, how much simulated time each long test adds on commits where
the selector picks it but it passes (local analysis only).

Usage (WSL): ARGUS_NO_RUNNER=1 python3 tools/long_waste.py SET_DIR [SET_DIR...]
"""
import json
import os
import subprocess
import sys

here = os.path.dirname(os.path.abspath(__file__))
repo = os.path.dirname(here)
LONG = {1490: "max_nodes", 1106: "tpcds", 1224: "gen_memory", 1107: "tpch", 1123: "opt_tpch"}


def cost(ms):
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


for d in sys.argv[1:]:
    env = dict(os.environ, ARGUS_DATA=d, ARGUS_NO_RUNNER="1")
    env.setdefault("ARGUS_SQLGLOT", "/root/argus-work/sqlglot")
    env.setdefault("ARGUS_MAP", os.path.join(repo, "map", "cov.sqlite"))
    r = subprocess.run([sys.executable, os.path.join(repo, "selector.py")], env=env, capture_output=True, text=True)
    tests = json.load(open(os.path.join(d, "tests.json")))["tests"]
    dur = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    ans = json.load(open(os.path.join(d, "answers.json")))["commits"]
    tot = 0
    per = {k: [0, 0, 0, 0.0] for k in LONG}  # selected, failing, passing, waste_s
    for line in r.stdout.splitlines():
        o = json.loads(line)
        sel = {t for w in o["workers"] for t in w}
        fail = set(ans[o["commit"]]["failing"])
        base = cost([dur[t] for t in sel])
        tot += base
        for k in LONG:
            if k in sel:
                per[k][0] += 1
                if k in fail:
                    per[k][1] += 1
                else:
                    per[k][2] += 1
                    per[k][3] += (base - cost([dur[t] for t in sel if t != k])) / 1000
    print(f"{os.path.basename(d.rstrip('/'))}: {len(ans)} commits, static {tot / 1000:.1f}s")
    for k, (s, f, p, w) in per.items():
        if s:
            print(f"  {LONG[k]:11s} selected {s:3d}  fails {f:3d}  passes {p:3d}  waste on passes {w:6.1f}s")
