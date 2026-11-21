"""Turns the isolated class-read pass (tools/build_iso_classes.sh) into map/classes.sqlite.

Inputs:
  WORKDIR/isocls/<test>.<pid>.json.z   one per process: classes, reads, creation edges
  WORKDIR/isocls/status.txt            pytest exit code per test under the tracker
  WORKDIR/isocls/nf/...                the same for tests rerun with data attributes only
  WORKDIR/sqlglot                      pinned checkout (hook functions are read from source)
  TESTS_JSON                           test numbers
Output tables:
  classes(id, name, tracked, mro, names, fn, un)  name = "module:Qual.Name"; mro = tracked
                                   bases in lookup order; names = wrapped attributes of the
                                   class; fn = those holding functions; un = attributes left
                                   unwrapped (properties, slots, nested classes)
  pairs(class_id, attr, blob_id)   tests whose lookup of attr passed through the class's __dict__
                                   position (not answered earlier in the owner's MRO)
  users(class_id, blob_id)         tests that read anything through the class or a subclass
  edges(class_id, attr, k_id, reader)  class k's creation read attr through the class;
                                       reader = "file:function" that did the read
  lambdas(path, line, blob_id)     tests that entered a lambda whose keyword is on that line
  blobs(id, bits)                  distinct zlib-compressed test bitmaps
  meta(key, value)                 hooks (vetted creation hooks and the names they touch),
                                   always (tests with no usable record), nofunc, ntests
Usage: python3 tools/make_classmap.py WORKDIR TESTS_JSON OUT_SQLITE
"""
import ast
import json
import os
import sqlite3
import sys
import zlib

work, tests_json, out = sys.argv[1:4]
tests = json.load(open(tests_json))["tests"]
valid = {t["number"] for t in tests}
ntests = len(tests)

# The creation hooks at the pinned commit, each read line by line (see classmap.py):
# reader key -> class whose subclasses it runs for.
HOOKS = {
    "sqlglot/dialects/dialect.py:_Dialect.__new__": "sqlglot.dialects.dialect:Dialect",
    "sqlglot/tokens.py:_TokenizerBase.__init_subclass__": "sqlglot.tokens:_TokenizerBase",
    "sqlglot/expressions/core.py:Expr.__init_subclass__": "sqlglot.expressions.core:Expr",
}

# Where a hook's whole effect of one attribute is known: attribute -> the attributes
# of the class being created that it computes from it. Expr.__init_subclass__ sets
# cls.required_args from cls.arg_types and nothing else from it. Hooks or
# attributes not listed here taint the whole class.
# _Dialect.__new__ uses the tokenizer's NUMERIC_ESCAPES only to pick the escape format
# it fills klass.ESCAPED_SEQUENCES with.
DERIVED = {
    "sqlglot/expressions/core.py:Expr.__init_subclass__": {"arg_types": ["required_args"]},
    "sqlglot/dialects/dialect.py:_Dialect.__new__": {"NUMERIC_ESCAPES": ["ESCAPED_SEQUENCES"]},
}


def hook_names(key):
    path, qual = key.split(":")
    tree = ast.parse(open(os.path.join(work, "sqlglot", path), encoding="utf-8").read())
    node = tree
    for part in qual.split("."):
        node = next(n for n in node.body if isinstance(n, (ast.ClassDef, ast.FunctionDef)) and n.name == part)
    names = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute):
            names.add(n.attr)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.isidentifier():
            names.add(n.value)
    return sorted(names)


def status(path):
    res = {}
    if os.path.exists(path):
        for line in open(path):
            n, rc = line.split()
            res[int(n)] = int(rc)
    return res


main_dir = os.path.join(work, "isocls")
nf_dir = os.path.join(main_dir, "nf")
st_main, st_nf = status(os.path.join(main_dir, "status.txt")), status(os.path.join(nf_dir, "status.txt"))
nofunc = sorted(n for n in valid if st_main.get(n) != 0 and st_nf.get(n) == 0)
always = sorted(n for n in valid if st_main.get(n) != 0 and st_nf.get(n) != 0)

cls_info = {}   # name -> dict(tracked, mro, names, fn)
pairs = {}      # (class name, attr) -> set of tests
users = {}      # class name -> set of tests
edges = set()   # (class name, attr, k name, reader)
lams = {}       # (path, line of the lambda keyword) -> set of tests that entered it
no_mon = set()  # tests with a process that could not record lambdas
conflicts = 0
nfiles = 0


def load(d, n, fname):
    global conflicts, nfiles
    try:
        data = json.loads(zlib.decompress(open(os.path.join(d, fname), "rb").read()))
    except Exception as e:
        print("unreadable", fname, e)
        return False
    nfiles += 1
    if not data.get("mon"):
        no_mon.add(n)
    for f, l in data.get("lam", []):
        lams.setdefault((f, l), set()).add(n)
    cl = data["classes"]
    if not cl:
        return True
    tracked = [c["id"] if c["t"] else None for c in cl]
    line = [[i] + c["mro"] for i, c in enumerate(cl)]
    for i, c in enumerate(cl):
        if not c["k"]:
            continue
        ent = {
            "tracked": bool(c["t"]),
            "mro": [tracked[j] for j in c["mro"] if tracked[j]],
            "names": set(c.get("names", [])),
            "fn": set(c.get("fn", [])),
            "un": set(c.get("un", [])),
        }
        have = cls_info.get(c["id"])
        if have is None:
            cls_info[c["id"]] = ent
        else:
            if have["tracked"] != ent["tracked"] or have["mro"] != ent["mro"]:
                conflicts += 1
                have["tracked"] = False  # not the same class in every process: do not trust it
            have["names"] |= ent["names"]
            have["fn"] |= ent["fn"]
            have["un"] |= ent["un"]
    prefix = {}

    def upto(o, k):
        key = (o, k)
        if key not in prefix:
            l = line[o]
            cut = l[: l.index(k) + 1] if k in l else l
            prefix[key] = [tracked[j] for j in cut if tracked[j]]
        return prefix[key]

    seen_owner = set()
    for o, name, k in data["reads"]:
        for c in upto(o, k):
            pairs.setdefault((c, name), set()).add(n)
        if o not in seen_owner:
            seen_owner.add(o)
            for j in line[o]:
                if tracked[j]:
                    users.setdefault(tracked[j], set()).add(n)
    for k, name in data["over"]:
        if tracked[k]:
            pairs.setdefault((tracked[k], name), set()).add(n)
            users.setdefault(tracked[k], set()).add(n)
    for K, o, name, k, fn, qn in data["edges"]:
        reader = f"{fn}:{qn.split('.<locals>')[0]}"
        for c in upto(o, k):
            edges.add((c, name, cl[K]["id"], reader))
    return True


def unfinished(d):
    """Tests with a process that started under the tracker but wrote no record."""
    res = set()
    if os.path.isdir(d):
        names = set(os.listdir(d))
        for f in names:
            if f.endswith(".start") and f[: -len(".start")] + ".json.z" not in names:
                res.add(int(f.split(".", 1)[0]))
    return res


lost_main, lost_nf = unfinished(main_dir), unfinished(nf_dir)
nofunc = sorted(n for n in valid if (st_main.get(n) != 0 or n in lost_main) and st_nf.get(n) == 0 and n not in lost_nf)
always = sorted(n for n in valid if (st_main.get(n) != 0 or n in lost_main) and n not in nofunc)
print(f"processes without a record: main pass {sorted(lost_main)} data-only pass {sorted(lost_nf)}")

got = set()
for d, ok in ((main_dir, lambda n: st_main.get(n) == 0 and n not in lost_main), (nf_dir, lambda n: n in nofunc)):
    if not os.path.isdir(d):
        continue
    for fname in sorted(os.listdir(d)):
        if not fname.endswith(".json.z"):
            continue
        n = int(fname.split(".", 1)[0])
        if n in valid and ok(n) and load(d, n, fname):
            got.add(n)
missing = sorted(valid - got - set(always))
always = sorted(set(always) | set(missing))  # a test with no record at all is always selected

for (_, _, k, _) in edges:
    cls_info.setdefault(k, {"tracked": False, "mro": [], "names": set(), "fn": set(), "un": set()})

if os.path.exists(out):
    os.remove(out)
dst = sqlite3.connect(out)
dst.executescript("""
create table classes(id integer primary key, name text unique, tracked integer, mro text, names text, fn text, un text);
create table blobs(id integer primary key, bits blob);
create table pairs(class_id integer, attr text, blob_id integer, primary key(class_id, attr)) without rowid;
create table users(class_id integer primary key, blob_id integer);
create table edges(class_id integer, attr text, k_id integer, reader text);
create table lambdas(path text, line integer, blob_id integer, primary key(path, line)) without rowid;
create table meta(key text primary key, value text);
create index edges_class on edges(class_id, attr);
""")
cid = {name: i + 1 for i, name in enumerate(sorted(cls_info))}
dst.executemany(
    "insert into classes values (?, ?, ?, ?, ?, ?, ?)",
    [
        (cid[name], name, int(c["tracked"]), json.dumps(c["mro"]), json.dumps(sorted(c["names"])),
         json.dumps(sorted(c["fn"])), json.dumps(sorted(c["un"] - c["names"])))
        for name, c in cls_info.items()
    ],
)
blob_ids = {}


def blob(ts):
    bits = bytearray((ntests + 7) // 8)
    for t in ts:
        bits[t >> 3] |= 1 << (t & 7)
    b = bytes(bits).rstrip(b"\x00")
    if b not in blob_ids:
        blob_ids[b] = len(blob_ids) + 1
    return blob_ids[b]


dst.executemany("insert into pairs values (?, ?, ?)", [(cid[c], a, blob(ts)) for (c, a), ts in sorted(pairs.items())])
dst.executemany("insert into lambdas values (?, ?, ?)", [(f, l, blob(ts)) for (f, l), ts in sorted(lams.items())])
dst.executemany("insert into users values (?, ?)", [(cid[c], blob(ts)) for c, ts in sorted(users.items())])
dst.executemany("insert into blobs values (?, ?)", [(i, zlib.compress(b, 9)) for b, i in blob_ids.items()])
dst.executemany("insert into edges values (?, ?, ?, ?)", sorted((cid[c], a, cid[k], r) for c, a, k, r in edges))
hooks = {key: {"owner": owner, "names": hook_names(key)} for key, owner in HOOKS.items()}
for key, table in DERIVED.items():
    hooks[key]["derived"] = table
dst.executemany(
    "insert into meta values (?, ?)",
    [("hooks", json.dumps(hooks)), ("always", json.dumps(always)), ("nofunc", json.dumps(nofunc)),
     ("ntests", str(ntests)), ("no_lambda", json.dumps(sorted(set(always) | no_mon)))],
)
dst.commit()
dst.execute("vacuum")
dst.close()
print(
    f"process files={nfiles} tests with a record={len(got)} always={always} nofunc={nofunc} "
    f"classes={len(cls_info)} tracked={sum(c['tracked'] for c in cls_info.values())} conflicts={conflicts} "
    f"lambdas={len(lams)} no_lambda={len(no_mon)} pairs={len(pairs)} distinct sets={len(blob_ids)} edges={len(edges)} size={os.path.getsize(out) / 1e6:.2f} MB"
)
