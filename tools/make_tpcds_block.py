"""Union of tools/build_tpcds_block.sh's coverage runs -> map/tpcds_block.json.xz:
every line test_execute_tpcds runs outside its parse/optimize step.

Usage: python3 tools/make_tpcds_block.py WORKDIR/tpb OUT.json.xz
"""
import glob
import json
import lzma
import os
import sqlite3
import sys

src, out = sys.argv[1], sys.argv[2]
PREFIX = "/work/sqlglot/"

status = [l.split() for l in open(os.path.join(src, "status.txt"))]
bad = [s for s in status if s[2] != "0"]
if bad:
    sys.exit(f"runs with nonzero exit: {bad[:10]}")
covs = glob.glob(os.path.join(src, "*.cov"))
if len(covs) != len(status):
    sys.exit(f"{len(covs)} coverage files for {len(status)} runs")
lines = {}
for cov in covs:
    con = sqlite3.connect(cov)
    files = dict(con.execute("select id, path from file"))
    for fid, bits in con.execute("select file_id, numbits from line_bits"):
        p = files[fid]
        if not p.startswith(PREFIX):
            continue
        s = lines.setdefault(p[len(PREFIX):], set())
        for i, byte in enumerate(bits):
            for b in range(8):
                if byte & (1 << b):
                    s.add(i * 8 + b)
    con.close()
doc = {"runs": len(covs), "lines": {f: sorted(v) for f, v in sorted(lines.items())}}
raw = json.dumps(doc, separators=(",", ":")).encode()
with lzma.open(out, "wb", preset=9 | lzma.PRESET_EXTREME) as fh:
    fh.write(raw)
print(f"{len(covs)} runs; {sum(len(v) for v in lines.values())} blocked lines in {len(lines)} files; "
      f"{len(raw)} bytes json, {os.path.getsize(out)} xz")
