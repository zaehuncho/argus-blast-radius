"""One TPC-DS pair of test_execute_tpcds with its optimize step replaced by the pinned
commit's optimized tree (from tpcds_check.py --dump), run under coverage by
tools/build_tpcds_block.sh: everything else the test runs for that pair, in a fresh
process with cold caches. Usage: python tpcds_block_harness.py DUMP_DIR PAIR
"""
import os
import pickle
import sys
from unittest import mock

sys.path.insert(0, os.getcwd())
import tests.test_executor as te  # noqa: E402
import sqlglot.executor as ex  # noqa: E402

dump_dir, i = sys.argv[1], int(sys.argv[2])
te.TestExecutor.setUpClass()
case = te.TestExecutor("test_execute_tpcds")
# the pool initializer's arguments cross a process boundary
te.initializer(pickle.loads(pickle.dumps(te.TPCDS_SCHEMA)), pickle.loads(pickle.dumps(case.tpcds_tables)))
if i >= 0:
    with open(os.path.join(dump_dir, f"{i}.pickle"), "rb") as fh:
        d = pickle.load(fh)
    optimized = d["optimized"]
    with mock.patch.object(ex, "optimize", lambda *a, **k: optimized):
        tables = {name: te._tables[name] for name in d["tables"]}
        table = ex.execute(optimized, schema=te._schema, tables=tables)
    table = pickle.loads(pickle.dumps(table))  # worker -> parent
    case.subtestHelper(i, table, tpch=False)
te.TestExecutor.tearDownClass()
