#!/usr/bin/env bash
# Lines test_execute_tpcds runs outside its optimize step (see tpcds_check.py): for
# each executed TPC-DS pair, a fresh process under coverage runs imports, setUpClass,
# the worker initializer and everything from execute's preamble to the duckdb
# comparison, with optimize stubbed to return the pinned commit's tree. Two hash
# seeds, plus one run with no pair. tools/make_tpcds_block.py writes the union.
# Usage (WSL/Linux with Docker): bash tools/build_tpcds_block.sh [workdir] [jobs]
set -euo pipefail
IMAGE=python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
W="${1:-$HOME/argus-work}"
JOBS="${2:-5}"
test -d "$W/sqlglot/.git" || { echo "run tools/build_coverage.sh first"; exit 1; }
cp "$REPO_DIR/requirements.lock" "$W/requirements.lock"
rm -rf "$W/tpb" && mkdir -p "$W/tpb/h"
cp "$REPO_DIR/tpcds_check.py" "$REPO_DIR/tools/tpcds_block_harness.py" "$W/tpb/h/"
cat > "$W/tpb/rc" <<'EOF'
[run]
source = sqlglot, tests
EOF
cat > "$W/tpb/one.sh" <<'EOF'
#!/usr/bin/env bash
p="$1"; s="$2"
cd /work/sqlglot
PYTHONHASHSEED="$s" SKIP_INTEGRATION=0 PYTHONPATH=/work/sqlglot COVERAGE_RCFILE=/work/tpb/rc \
  COVERAGE_FILE="/work/tpb/${p}_$s.cov" timeout 600 \
  python -m coverage run /work/tpb/h/tpcds_block_harness.py /work/tpb/dump "$p" > "/work/tpb/${p}_$s.log" 2>&1
echo "$p $s $?" >> /work/tpb/status.txt
EOF
chmod +x "$W/tpb/one.sh"
docker run --rm -v "$W":/work "$IMAGE" bash -c "
  pip install -q --root-user-action=ignore -r /work/requirements.lock 'coverage==7.*' &&
  cd /work/sqlglot && PYTHONPATH=/work/sqlglot python /work/tpb/h/tpcds_check.py --dump /work/tpb/dump &&
  { for s in 0 1; do echo -1 \$s; for f in /work/tpb/dump/*.pickle; do b=\$(basename \$f .pickle); echo \$b \$s; done; done; } |
  xargs -P $JOBS -L 1 /work/tpb/one.sh"
echo "runs: $(wc -l < "$W/tpb/status.txt")  nonzero exits: $(awk '$3!=0' "$W/tpb/status.txt" | wc -l)"
