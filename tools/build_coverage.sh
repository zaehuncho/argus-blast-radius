#!/usr/bin/env bash
# Builds a per-test line coverage database for sqlglot at the pinned commit.
# Runs sqlglot's tests once, in the same base image the scorer labeled with,
# recording which lines each test executes (pytest-cov test contexts).
# Usage (in WSL/Linux with Docker): bash tools/build_coverage.sh [workdir]
set -euo pipefail
PIN=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
IMAGE=python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
W="${1:-$HOME/argus-work}"
mkdir -p "$W"
cd "$W"
if [ ! -d sqlglot/.git ]; then
  git clone -q https://github.com/tobymao/sqlglot.git sqlglot
fi
git -C sqlglot checkout -q "$PIN"
cp "$REPO_DIR/requirements.lock" "$W/requirements.lock"
cat > "$W/coveragerc" <<'EOF'
[run]
source = sqlglot, tests
data_file = /work/coverage.db
EOF
cp "$REPO_DIR/tools/open_tracker.py" "$W/open_tracker.py"
rm -f "$W"/coverage.db* "$W/opens.json"
docker run --rm -v "$W":/work -w /work/sqlglot \
  -e SKIP_INTEGRATION=0 -e PYTHONPATH=/work/sqlglot:/work -e COVERAGE_RCFILE=/work/coveragerc \
  -e ARGUS_ROOT=/work/sqlglot -e ARGUS_OPENS_OUT=/work/opens.json \
  "$IMAGE" bash -c '
    pip install -q --root-user-action=ignore -r /work/requirements.lock "coverage==7.*" "pytest-cov==7.*" &&
    python -m pytest -p no:cacheprovider -p open_tracker -o addopts="" -q \
      --cov --cov-config=/work/coveragerc --cov-context=test --cov-report= \
      --junitxml=/work/junit.xml tests > /work/pytest.log 2>&1
    echo "pytest exit=$?" >> /work/pytest.log'
ls -la "$W/opens.json"
tail -3 "$W/pytest.log"
ls -la "$W"/coverage.db*
