#!/usr/bin/env bash
# Per-test isolated record of the sqlglot class attributes each test reads
# (tools/classes_site/sitecustomize.py). Every test runs alone in a fresh
# process; spawned workers inherit PYTHONPATH and record too.
# Usage (WSL/Linux with Docker): bash tools/build_iso_classes.sh [workdir] [jobs]
# Env: ARGUS_IMAGE overrides the image (it must already hold requirements.lock).
# Output: $W/isocls/<test>.<pid>.json.z and $W/isocls/status.txt; feed to tools/make_classmap.py.
set -euo pipefail
BASE=python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
IMAGE="${ARGUS_IMAGE:-$BASE}"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
W="${1:-$HOME/argus-work}"
JOBS="${2:-8}"
test -d "$W/sqlglot/.git" || { echo "run tools/build_coverage.sh first"; exit 1; }
cp "$REPO_DIR/requirements.lock" "$W/requirements.lock"
rm -rf "$W/cls_site" "$W/isocls" && mkdir -p "$W/cls_site" "$W/isocls"
cp "$REPO_DIR/tools/classes_site/sitecustomize.py" "$W/cls_site/sitecustomize.py"
python3 -c "import json,sys; [print(t['number'], t['id']) for t in json.load(open(sys.argv[1]))['tests']]" \
  "$REPO_DIR/sample/tests.json" > "$W/isocls_tests.txt"
# classes the tracker must leave alone: any decorator other than mypyc's no-op markers
python3 - "$W/sqlglot" > "$W/cls_site/skip.json" <<'EOF'
import ast, json, os, sys
root = sys.argv[1]
bad = []
for d, _, fs in os.walk(os.path.join(root, "sqlglot")):
    for f in fs:
        if not f.endswith(".py"):
            continue
        p = os.path.join(d, f)
        mod = os.path.relpath(p, root)[:-3].replace(os.sep, ".")
        if mod.endswith(".__init__"):
            mod = mod[: -len(".__init__")]

        def walk(body, prefix):
            for n in body:
                if isinstance(n, ast.ClassDef):
                    q = prefix + n.name
                    for dec in n.decorator_list:
                        ok = (isinstance(dec, ast.Name) and dec.id == "trait") or (
                            isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name) and dec.func.id == "mypyc_attr"
                        )
                        if not ok:
                            bad.append(f"{mod}:{q}")
                    walk(n.body, q + ".")
                elif isinstance(n, (ast.If, ast.Try, ast.With)):
                    for part in ("body", "orelse", "finalbody"):
                        walk(getattr(n, part, []), prefix)

        walk(ast.parse(open(p, encoding="utf-8").read()).body, "")
print(json.dumps(sorted(set(bad))))
EOF
cat > "$W/iso_cls_one.sh" <<'EOF'
#!/usr/bin/env bash
# $1 = output dir under /work, $2 = 1 to wrap data attributes only, then test number and node id
out="/work/$1"; nofunc="$2"; shift 2
n="$1"; shift; nodeid="$*"
cd /work/sqlglot
[ "$nofunc" = 1 ] && export ARGUS_CLS_NOFUNC=1
SKIP_INTEGRATION=0 PYTHONPATH=/work/cls_site:/work/sqlglot ARGUS_CLS_DIR="$out" ARGUS_TEST_NUM="$n" \
  ARGUS_CLS_SKIP=/work/cls_site/skip.json ARGUS_ROOT=/work/sqlglot \
  timeout 1800 python -m pytest -p no:cacheprovider -o addopts="" -q "$nodeid" > "$out/$n.log" 2>&1
rc=$?
echo "$n $rc" >> "$out/status.txt"
EOF
chmod +x "$W/iso_cls_one.sh"
if [ "$IMAGE" = "$BASE" ]; then
  PREP="pip install -q --root-user-action=ignore -r /work/requirements.lock &&"
else
  PREP=""
fi
LIST="${ARGUS_CLS_TESTS:-/work/isocls_tests.txt}"
time docker run --rm -v "$W":/work "$IMAGE" bash -c "
  $PREP cd /work && xargs -P $JOBS -L 1 /work/iso_cls_one.sh isocls 0 < $LIST"
echo "runs: $(wc -l < "$W/isocls/status.txt")  nonzero: $(awk '$2!=0' "$W/isocls/status.txt" | wc -l)  files: $(ls "$W/isocls" | grep -c 'json.z')"
# Tests that fail only because methods are wrapped (unittest.mock autospec looks
# at the class __dict__) run again with data attributes wrapped only. The retry
# directory sits inside isocls, which this script recreated above.
mkdir -p "$W/isocls/nf"
awk '$2!=0 {print $1}' "$W/isocls/status.txt" | sort -n > "$W/isocls/failed.txt"
if [ -s "$W/isocls/failed.txt" ]; then
  awk 'NR==FNR {f[$1]=1; next} ($1 in f)' "$W/isocls/failed.txt" "$W/isocls_tests.txt" > "$W/isocls/retry.txt"
  docker run --rm -v "$W":/work "$IMAGE" bash -c "
    $PREP cd /work && xargs -P $JOBS -L 1 /work/iso_cls_one.sh isocls/nf 1 < /work/isocls/retry.txt"
  echo "data-only retries: $(wc -l < "$W/isocls/nf/status.txt")  still failing: $(awk '$2!=0' "$W/isocls/nf/status.txt" | wc -l)"
fi
