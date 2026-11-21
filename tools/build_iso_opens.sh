#!/usr/bin/env bash
# Per-test isolated record of data files read under tests/. Shared class setup
# (setUpClass) loads fixtures once per class in a normal run, so only the first
# test gets credit; running each test alone credits every test that needs them.
# Usage (WSL/Linux with Docker): bash tools/build_iso_opens.sh [workdir] [jobs]
set -euo pipefail
IMAGE=python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
W="${1:-$HOME/argus-work}"
JOBS="${2:-14}"
test -d "$W/sqlglot/.git" || { echo "run tools/build_coverage.sh first"; exit 1; }
cp "$REPO_DIR/requirements.lock" "$W/requirements.lock"
rm -rf "$W/opens_site" "$W/isoopens" && mkdir -p "$W/opens_site" "$W/isoopens"
cp "$REPO_DIR/tools/opens_site/sitecustomize.py" "$W/opens_site/sitecustomize.py"
python3 -c "import json,sys; [print(t['number'], t['id']) for t in json.load(open(sys.argv[1]))['tests']]" \
  "$REPO_DIR/sample/tests.json" > "$W/iso_tests.txt"
cat > "$W/iso_opens_one.sh" <<'EOF'
#!/usr/bin/env bash
n="$1"; shift; nodeid="$*"
cd /work/sqlglot
SKIP_INTEGRATION=0 PYTHONPATH=/work/opens_site:/work/sqlglot ARGUS_OPENS_DIR=/work/isoopens ARGUS_TEST_NUM="$n" \
  ARGUS_ROOT=/work/sqlglot timeout 900 python -m pytest -p no:cacheprovider -o addopts="" -q "$nodeid" > /dev/null 2>&1
echo "$n $?" >> /work/isoopens/status.txt
EOF
chmod +x "$W/iso_opens_one.sh"
docker run --rm -v "$W":/work "$IMAGE" bash -c "
  pip install -q --root-user-action=ignore -r /work/requirements.lock &&
  cd /work && xargs -P $JOBS -L 1 /work/iso_opens_one.sh < /work/iso_tests.txt"
echo "runs: $(wc -l < "$W/isoopens/status.txt")  nonzero: $(awk '$2!=0' "$W/isoopens/status.txt" | wc -l)  files: $(ls "$W/isoopens" | grep -c txt)"
