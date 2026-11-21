"""Ground truth versus tpcds_check.py, inside the production image (local only):
for each patch, the full test_execute_tpcds through runner.py, and the check.
Usage: python /t/validate_tpcds_check.py COMMITS_DIR ID [ID...]   (prints JSON lines)
"""
import json
import os
import sys

sys.path.insert(0, "/app")
import runner  # noqa: E402

NODE = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
d = sys.argv[1]
for cid in sys.argv[2:]:
    text = open(os.path.join(d, cid + ".patch"), encoding="utf-8").read()
    files = runner.files_from_patch(text)
    chk = runner.run_script(files, ["/app/tpcds_check.py", "--check", "/app/map/tpcds_prints.json"], timeout_s=60)
    res = runner.run_candidates(files, [NODE], timeout_s=600, pool_workers=2)
    truth = "fail" if NODE in res["known_failing"] else "pass" if NODE in res["passed"] else res["status"]
    print(json.dumps({"cid": cid, "truth": truth, "check_exit": chk["exitcode"], "check_status": chk["status"],
                      "check_cpu": round(chk["cpu_s"], 2), "tail": chk["log_tail"][-300:]}), flush=True)
