"""Stage two of the TPC-DS check against known ground truth, inside the production
image (local only): tpcds_check.py with K differing queries allowed, then
test_execute_tpcds narrowed to those queries, as selector.py does.
Usage: python /t/validate_tpcds_stage2.py COMMITS_DIR K ID:TRUTH [ID:TRUTH...]
"""
import json
import os
import re
import sys

sys.path.insert(0, "/app")
import runner  # noqa: E402

NODE = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
d, k = sys.argv[1], sys.argv[2]
for item in sys.argv[3:]:
    cid, truth = item.split(":")
    files = runner.files_from_patch(open(os.path.join(d, cid + ".patch"), encoding="utf-8").read())
    chk = runner.run_script(files, ["/app/tpcds_check.py", "--check", "/app/map/tpcds_prints.json", k], timeout_s=60)
    m = re.search(r"TPCDS_DIFFER ([0-9,]+)", chk["log_tail"])
    verdict, run_cpu = ("drop" if chk["exitcode"] == 0 else "keep"), 0.0
    if chk["exitcode"] == 4 and m:
        r2 = runner.run_candidates(files, [NODE], timeout_s=120, extra_env={"ARGUS_TPCDS_ONLY": m.group(1)})
        run_cpu = r2["cpu_s"]
        verdict = "drop" if r2["failing"] is not None and NODE in r2["passed"] else "keep"
    print(json.dumps({"cid": cid, "truth": truth, "exit": chk["exitcode"], "differ": m.group(1) if m else None,
                      "verdict": verdict, "check_cpu": round(chk["cpu_s"], 2), "run_cpu": round(run_cpu, 2)}), flush=True)
