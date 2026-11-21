"""Decides test_execute_tpcds without executing it, when a patch can only change the
optimizer step of its queries.

test_execute_tpcds, per TPC-DS fixture pair: the parent parses the SQL and pickles it
to a pool worker; the worker unpickles it, collects its tables (find_tables) and calls
sqlglot.executor.execute: ensure_schema, then optimize(..., leave_tables_isolated=True),
then plan, run, and return a Table that the parent compares with duckdb's answer.
Pairs without "execute: true" stop after the unpickle.

map/tpcds_block.json (tools/build_tpcds_block.sh) lists every line the test runs
outside that first part: imports, setUpClass, the worker initializer, and, per pair
in a fresh process with cold caches, everything from execute's preamble on, with
optimize stubbed to return the pinned commit's optimized tree. When a patch changes
none of those lines, everything after optimize gets the same input and runs the same
code, so the test passes if every pair's optimized tree, table set and pickling come
out as on the pinned commit.

  python tpcds_check.py --build OUT.json   fingerprints at the pinned commit (Docker build)
  python tpcds_check.py --check MAP.json [K]   exit 0: identical; 4: 1..K pairs differ
                                           (printed as "TPCDS_DIFFER i,j"); 3: more differ
  python tpcds_check.py --dump DIR         pickles each optimized tree (map building)
Run with the (patched) sqlglot checkout as the working directory and on sys.path.
"""
import hashlib
import json
import os
import pickle
import sys

sys.setrecursionlimit(20000)
sys.path.insert(0, os.getcwd())


def fingerprint(root, Expr):
    """Everything an Expr tree carries, in a fixed order: class, every arg (lists,
    dicts, plain values), comments, type, meta, whether a hash is cached, each
    node's parent / arg_key / index, and which nodes are the same object."""
    ids = {}
    out = []
    add = out.append

    def node(o):
        if id(o) in ids:
            add(f"@{ids[id(o)]}")
            return
        ids[id(o)] = len(ids)
        add(f"({type(o).__module__}.{type(o).__qualname__}")
        p = o.parent
        add("parent:" + ("N" if p is None else str(ids.get(id(p), "?"))))
        add("key:" + repr(o.arg_key) + " idx:" + repr(o.index) + " hash:" + str(o._hash is not None))
        add("comments:")
        value(o.comments)
        add("type:")
        value(o._type)
        add("meta:")
        value(o._meta)
        for k, v in o.args.items():
            add("." + repr(k))
            value(v)
        add(")")

    def value(v):
        if isinstance(v, Expr):
            node(v)
        elif isinstance(v, list):
            add("[")
            for x in v:
                value(x)
            add("]")
        elif isinstance(v, tuple):
            add("t(")
            for x in v:
                value(x)
            add(")")
        elif isinstance(v, dict):
            add("{")
            for k, x in v.items():
                add(repr(k))
                value(x)
            add("}")
        elif isinstance(v, (set, frozenset)):
            add("s" + repr(sorted(repr(x) for x in v)))
        elif v is None or isinstance(v, (bool, int, float, str, bytes)):
            add(type(v).__name__ + ":" + repr(v))
        else:
            add(type(v).__module__ + "." + type(v).__qualname__ + ":" + repr(v))

    value(root)
    return hashlib.sha256("\x00".join(out).encode("utf-8", "surrogatepass")).hexdigest()


class Differs(Exception):
    pass


def compute(dump_dir=None, want=None, max_diff=0, differ=None):
    """Fingerprints every executed pair. With want, collects the pairs that do not
    match it in `differ` and raises Differs once more than max_diff of them do."""
    from tests.helpers import TPCDS_SCHEMA, load_sql_fixture_pairs
    from sqlglot import find_tables, parse_one
    from sqlglot.expressions.core import Expr
    from sqlglot.optimizer import optimize
    from sqlglot.schema import ensure_schema

    schema = pickle.loads(pickle.dumps(TPCDS_SCHEMA))  # the worker initializer's copy
    pairs = list(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))
    # the parent parses and pickles every pair before any worker result comes back
    parsed = [pickle.loads(pickle.dumps(parse_one(sql))) for _, sql, _ in pairs]
    got = {}
    for i, (meta, _, _) in enumerate(pairs):
        if not meta.get("execute"):
            continue
        expression = parsed[i]
        tables = sorted({t.name for t in find_tables(expression)})
        optimized = optimize(expression, ensure_schema(schema, dialect=None), leave_tables_isolated=True, dialect=None)
        got[str(i)] = [fingerprint(optimized, Expr), tables]
        if want is not None and want.get(str(i)) != got[str(i)]:
            differ.append(i)
            if len(differ) > max_diff:
                raise Differs(i)
        if dump_dir:
            with open(os.path.join(dump_dir, f"{i}.pickle"), "wb") as fh:
                pickle.dump({"optimized": optimized, "tables": tables}, fh)
    if want is not None and set(got) != set(want):
        raise Differs("pair set")
    return got


def main(argv):
    if argv[1] == "--build":
        got = compute()
        with open(argv[2], "w", encoding="utf-8") as fh:
            json.dump({"pairs": got}, fh)
        print(f"tpcds_check: {len(got)} executed pairs fingerprinted")
        return 0
    if argv[1] == "--dump":
        os.makedirs(argv[2], exist_ok=True)
        compute(argv[2])
        return 0
    with open(argv[2], encoding="utf-8") as fh:
        want = json.load(fh)["pairs"]
    max_diff = int(argv[3]) if len(argv) > 3 else 0
    differ = []
    try:
        compute(want=want, max_diff=max_diff, differ=differ)
    except Differs as e:
        print(f"tpcds_check: pair {e} differs, {len(differ)} so far", file=sys.stderr)
        return 3
    if differ:
        # only these pairs can behave differently: test_execute_tpcds narrowed to them decides
        print("TPCDS_DIFFER " + ",".join(map(str, differ)), flush=True)
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
