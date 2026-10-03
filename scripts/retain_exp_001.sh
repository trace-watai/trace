#!/usr/bin/env bash
# Retain exp_001 after `experiment record`: copy the runs dir the branch stage
# wrote into, and copy result.json, report.md and repair_effectiveness.json up
# beside the plan, so scripts/regenerate_exp_001.sh can recompute them offline.
# The recorded copies stay where record wrote them.
#
# Usage: scripts/retain_exp_001.sh <runs_dir> [retained_dir]
#   runs_dir      the --runs-dir every branch and record command of exp_001 used
#   retained_dir  defaults to docs/acceptance/experiments/exp_001_replay_validity
#
# The plan is never touched. A file under either directory that looks like a
# provider credential stops the copy before anything is written.
set -euo pipefail
cd "$(dirname "$0")/.."

RUNS="${1:?usage: scripts/retain_exp_001.sh <runs_dir> [retained_dir]}"
RETAINED="${2:-docs/acceptance/experiments/exp_001_replay_validity}"
PYTHON="${PYTHON:-python}"
PLAN="$RETAINED/experiment.json"

[ -f "$PLAN" ] || { echo "error: no plan at $PLAN" >&2; exit 2; }
ID="$("$PYTHON" -c 'import json, sys; print(json.load(open(sys.argv[1]))["experiment_id"])' "$PLAN")"
RECORDED="$RUNS/experiments/$ID"
for file in result.json report.md repair_effectiveness.json; do
  [ -f "$RECORDED/$file" ] || {
    echo "error: $RECORDED/$file not found; run experiment record first" >&2
    exit 2
  }
done

# record writes the plan it read beside the result; it must be this plan.
"$PYTHON" - "$PLAN" "$RECORDED/experiment.json" <<'EOF'
import json
import sys

retained, recorded = (json.load(open(path)) for path in sys.argv[1:])
if retained != recorded:
    sys.exit(f"error: {sys.argv[2]} differs from {sys.argv[1]}; the result was recorded from another plan")
EOF

KEY='(^|[^A-Za-z0-9])(AIza[0-9A-Za-z_-]{30,}|sk-(ant-)?[A-Za-z0-9_-]{20,}|Bearer [A-Za-z0-9._-]{20,})'
if grep -rIlE "$KEY" "$RUNS" "$RETAINED"; then
  echo "error: the files above look like they hold a credential; nothing was retained" >&2
  exit 1
fi

rm -rf "$RETAINED/runs"
mkdir -p "$RETAINED/runs"
for entry in "$RUNS"/*; do
  # The experiments folder holds the three files copied up below, and a copy of the plan.
  [ "$(basename "$entry")" = experiments ] && continue
  cp -R "$entry" "$RETAINED/runs/"
done
cp "$RECORDED/result.json" "$RECORDED/report.md" "$RECORDED/repair_effectiveness.json" "$RETAINED/"
echo "retained $ID from $RUNS into $RETAINED"
