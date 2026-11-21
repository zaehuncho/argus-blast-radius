#!/usr/bin/env bash
# Runs IMAGE under the scorer's flags (Makefile RUN_FLAGS) on a commit set and scores it,
# plus the container's total CPU (its cgroup's cpu.stat, read at the end of the run).
# Usage: bash tools/container_eval.sh IMAGE SET_DIR [OUT.jsonl]
set -euo pipefail
IMAGE="$1"; SET="$2"; OUT="${3:-/tmp/argus-eval-$$.jsonl}"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
docker run --rm --network none --cpus 2 --memory 4096m --memory-swap 4096m --pids-limit 256 \
  --read-only --tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL --security-opt no-new-privileges \
  --user 65534:65534 --env HOME=/tmp ${EVAL_ENV:-} \
  --volume "$SET/commits":/data/commits:ro --volume "$SET/tests.json":/data/tests.json:ro \
  --entrypoint sh "$IMAGE" -c 'python /app/selector.py; echo "CGROUP_CPU_USEC $(grep usage_usec /sys/fs/cgroup/cpu.stat)" >&2' \
  > "$OUT" 2> "$OUT.log"
python3 "$REPO_DIR/score.py" "$SET" "$SET/answers.json" "$OUT" | head -2
awk '/^CGROUP_CPU_USEC/ {printf "CPU:       %.2f s (container cgroup)\n", $3/1e6}' "$OUT.log"
