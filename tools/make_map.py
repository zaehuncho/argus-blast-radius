"""Turns the coverage builds' outputs into the compact map the selector ships with.

Inputs (from tools/build_coverage.sh and tools/build_iso_coverage.sh):
  coverage.db   one full test run with pytest-cov test contexts
  iso/<n>.cov   one coverage file per test, each test run alone in a fresh process
  opens.json    files under tests/ each test read (full run)
  tests.json    test numbers
Output: map/cov.sqlite with
  files(id, path)                  repository-relative source paths
  blobs(id, bits)                  distinct zlib-compressed coverage numbits
  lines(file_id, test, blob_id)    lines each test executed in each file (union of both runs)
  opens(test, path)                data files each test read under tests/
Usage: python3 tools/make_map.py WORKDIR TESTS_JSON OUT_SQLITE
"""
import json
import os
import sqlite3
import sys
import zlib

work, tests_json, out = sys.argv[1:4]
root = "/work/sqlglot/"

tests = json.load(open(tests_json))["tests"]
num = {t["id"]: t["number"] for t in tests}
by_file = {}
for t in tests:
    by_file.setdefault(t["file"], []).append(t["number"])


def bor(a, b):
    if len(a) < len(b):
        a, b = b, a
    return bytes(x | y for x, y in zip(a, b)) + a[len(b):]


per = {}  # (path, test) -> bits


def add(path, test, bits):
    key = (path, test)
    per[key] = bor(per[key], bytes(bits)) if key in per else bytes(bits)


# 1. the full run, credited per test context
src = sqlite3.connect(os.path.join(work, "coverage.db"))
ctx = {}
for cid, name in src.execute("select id, context from context"):
    nodeid = name.rsplit("|", 1)[0] if "|" in name else name
    if nodeid in num:
        ctx[cid] = num[nodeid]
paths = {fid: p[len(root):] for fid, p in src.execute("select id, path from file") if p.startswith(root)}
for fid, cid, bits in src.execute("select file_id, context_id, numbits from line_bits"):
    if fid in paths and cid in ctx:
        add(paths[fid], ctx[cid], bits)

# 2. isolated runs: everything the test's own process executed
iso_dir = os.path.join(work, "iso")
iso_count = 0
for t in tests:
    f = os.path.join(iso_dir, f"{t['number']}.cov")
    if not os.path.exists(f):
        continue
    iso_count += 1
    db = sqlite3.connect(f)
    ip = {fid: p[len(root):] for fid, p in db.execute("select id, path from file") if p.startswith(root)}
    for fid, bits in db.execute("select file_id, numbits from line_bits"):
        if fid in ip:
            add(ip[fid], t["number"], bits)
    db.close()

if os.path.exists(out):
    os.remove(out)
dst = sqlite3.connect(out)
dst.executescript("""
create table files(id integer primary key, path text unique);
create table blobs(id integer primary key, bits blob);
create table lines(file_id integer, test integer, blob_id integer);
create table opens(test integer, path text);
create index lines_file on lines(file_id);
create index opens_path on opens(path);
""")
file_ids, blob_ids, rows = {}, {}, []
for (path, test), bits in per.items():
    bits = bits.rstrip(b"\x00")
    if not bits:
        continue
    fid = file_ids.setdefault(path, len(file_ids) + 1)
    bid = blob_ids.setdefault(bits, len(blob_ids) + 1)
    rows.append((fid, test, bid))
dst.executemany("insert into files values (?, ?)", [(i, p) for p, i in file_ids.items()])
dst.executemany("insert into blobs values (?, ?)", [(i, zlib.compress(b, 9)) for b, i in blob_ids.items()])
dst.executemany("insert into lines values (?, ?, ?)", rows)

opens = json.load(open(os.path.join(work, "opens.json")))
orows = set()
for key, ps in opens.items():
    if key.startswith("collect:"):
        targets = by_file.get(key[len("collect:"):].split("::")[0], [])
    else:
        targets = [num[key]] if key in num else []
    for t in targets:
        for p in ps:
            orows.add((t, p))
dst.executemany("insert into opens values (?, ?)", sorted(orows))
dst.commit()
dst.execute("vacuum")
dst.close()
print(f"iso runs={iso_count} files={len(file_ids)} line rows={len(rows)} distinct blobs={len(blob_ids)} "
      f"opens={len(orows)} size={os.path.getsize(out) / 1e6:.2f} MB")
