#!/usr/bin/env bash
# Per-test isolated coverage: every test runs alone in a fresh process, and every
# line that process executes (imports, caches, class setup included) is credited
# to that test. Shared caches in one big run credit code only to the first test
# that fills them; isolation removes that blind spot.
# Usage (WSL/Linux with Docker): bash tools/build_iso_coverage.sh [workdir] [jobs]
set -euo pipefail
IMAGE=python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
W="${1:-$HOME/argus-work}"
JOBS="${2:-14}"
test -d "$W/sqlglot/.git" || { echo "run tools/build_coverage.sh first"; exit 1; }
cp "$REPO_DIR/requirements.lock" "$W/requirements.lock"
python3 -c "import json,sys; [print(t['number'], t['id']) for t in json.load(open(sys.argv[1]))['tests']]" \
  "$REPO_DIR/sample/tests.json" > "$W/iso_tests.txt"
# sqlglot's optimizer and executor tests run their work in a spawned process
# pool, so coverage must follow child processes (multiprocessing + subprocess).
cat > "$W/iso_coveragerc" <<'EOF'
[run]
source = sqlglot, tests
parallel = true
concurrency = multiprocessing, thread
patch = subprocess
EOF
cat > "$W/iso_one.sh" <<'EOF'
#!/usr/bin/env bash
# $1 = test number, $2 = node id
n="$1"; shift; nodeid="$*"
d="/work/iso/d$n"; mkdir -p "$d"
cd /work/sqlglot
SKIP_INTEGRATION=0 PYTHONPATH=/work/sqlglot COVERAGE_RCFILE=/work/iso_coveragerc COVERAGE_FILE="$d/c" \
  timeout 900 python -m coverage run -m pytest -p no:cacheprovider -o addopts="" -q "$nodeid" > "/work/iso/$n.log" 2>&1
rc=$?
COVERAGE_RCFILE=/work/iso_coveragerc COVERAGE_FILE="$d/c" python -m coverage combine -q "$d" >> "/work/iso/$n.log" 2>&1
mv "$d/c" "/work/iso/$n.cov" 2>/dev/null; rm -rf "$d"
echo "$n $rc" >> /work/iso/status.txt
EOF
chmod +x "$W/iso_one.sh"
rm -rf "$W/iso" && mkdir -p "$W/iso"
docker run --rm -v "$W":/work "$IMAGE" bash -c "
  pip install -q --root-user-action=ignore -r /work/requirements.lock 'coverage==7.*' &&
  cd /work && xargs -P $JOBS -L 1 /work/iso_one.sh < /work/iso_tests.txt"
echo "runs: $(wc -l < "$W/iso/status.txt")  nonzero exits: $(awk '\$2!=0' "$W/iso/status.txt" | wc -l)"
