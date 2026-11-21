"""Commits whose selection relies on the class map's import-check rule or the module
constant rule: selection size,
cost, and whether every failing test is selected (local validation only).
Usage (WSL): python3 tools/import_rule_eval.py SET_DIR [SET_DIR...]
  (answers from SET_DIR/answers.json, or RES_DIR given as SET_DIR=RES_DIR with harness results)
"""
import glob
import json
import os
import sys

here = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("ARGUS_MAP", os.path.join(os.path.dirname(here), "map", "cov.sqlite"))
os.environ.setdefault("ARGUS_SQLGLOT", "/root/argus-work/sqlglot")
sys.path.insert(0, os.path.dirname(here))
import selector  # noqa: E402

tests = json.load(open(os.path.join(os.path.dirname(here), "sample", "tests.json")))["tests"]
dur = {t["number"]: round(t["seconds"] * 1000) for t in tests}
every = set(dur)
nodeid_num = {t["id"]: t["number"] for t in tests}
tests_by_file = {}
for t in tests:
    tests_by_file.setdefault(t["file"], []).append(t["number"])
cov = selector.Map(selector.MAP)
bad = total = 0
for arg in sys.argv[1:]:
    setdir, _, resdir = arg.partition("=")
    if resdir:
        ans = {}
        for f in glob.glob(os.path.join(resdir, "*.json")):
            r = json.load(open(f))
            if not r.get("flags") and r.get("rc") in (0, 1):
                ans[os.path.basename(f)[:-5]] = set(r["failing"])
    else:
        ans = {c: set(v["failing"]) for c, v in json.load(open(os.path.join(setdir, "answers.json")))["commits"].items()}
    for name in sorted(os.listdir(os.path.join(setdir, "commits"))):
        cid = name[:-6]
        if cid not in ans:
            continue
        text = open(os.path.join(setdir, "commits", name), encoding="utf-8").read()
        reasons, checks = [], []
        sel = selector.select(text, every, cov, tests_by_file, nodeid_num, reasons, checks) & every
        if not checks and not any("module constants" in r for r in reasons):
            continue
        total += 1
        miss = ans[cid] - sel
        bad += bool(miss)
        cost = selector.cost_ms(selector.pack(sel, dur), dur) / 1000
        print(f"{cid} sel={len(sel):5d} cost={cost:6.1f}s fail={len(ans[cid]):3d} {'MISS ' + str(sorted(miss)[:5]) if miss else 'ok'} | {'; '.join(reasons)[:140]}")
print(f"{total} commits use the rule; {bad} would miss")
