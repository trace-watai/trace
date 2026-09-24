#!/usr/bin/env bash
# Recompute exp_001's result.json and repair_effectiveness.json from the
# retained artifacts, with no network, and diff them against the retained
# copies. Exits 0 when both match, 1 when either differs, 2 on a usage error.
#
# Usage: scripts/regenerate_exp_001.sh [retained_dir] [--allow-drift]
#   retained_dir  defaults to docs/acceptance/experiments/exp_001_replay_validity
#   --allow-drift record even though the frozen set moved since the plan was
#                 frozen, and leave the frozen-set fields and the decision,
#                 which drift rewrites, out of the diff
#
# The recomputation runs `experiment record` on a scratch copy of the retained
# runs dir with the retained condition-to-batch map, so every number comes from
# the retained batch summaries and each run's verifier_result.json. No provider
# key is passed through and every socket is refused. The diff leaves out
# finished_at, the one timestamp in either file, and report_path, which names
# the runs dir the result was recorded in.
set -euo pipefail
cd "$(dirname "$0")/.."

RETAINED="docs/acceptance/experiments/exp_001_replay_validity"
ALLOW_DRIFT=0
for arg in "$@"; do
  case "$arg" in
    --allow-drift) ALLOW_DRIFT=1 ;;
    -*) echo "error: unknown option $arg" >&2; exit 2 ;;
    *) RETAINED="$arg" ;;
  esac
done
PYTHON="${PYTHON:-python}"

for file in experiment.json result.json repair_effectiveness.json; do
  [ -f "$RETAINED/$file" ] || {
    echo "error: $RETAINED/$file not found; exp_001 has not been retained there" >&2
    exit 2
  }
done
[ -d "$RETAINED/runs" ] || { echo "error: $RETAINED/runs not found" >&2; exit 2; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cp -R "$RETAINED/runs" "$WORK/runs"
rm -rf "$WORK/runs/experiments"

env -u GEMINI_API_KEY -u GOOGLE_API_KEY -u ANTHROPIC_API_KEY -u OPENAI_API_KEY \
  "$PYTHON" - "$RETAINED" "$WORK/runs" "$ALLOW_DRIFT" <<'EOF'
import difflib
import json
import socket
import sys
from pathlib import Path


def no_network(*args, **kwargs):
    raise RuntimeError("regenerate_exp_001 must not reach the network")


socket.socket = no_network
socket.create_connection = no_network

from trace_harness.cli import main  # noqa: E402

retained, runs, allow_drift = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3] == "1"
kept = json.loads((retained / "result.json").read_text(encoding="utf-8"))
record = [
    "--runs-dir", str(runs), "experiment", "record", str(retained / "experiment.json"),
    "--decision", kept["decision"], "--decided-by", kept["decided_by"],
]
for name, batch_id in kept["condition_batches"].items():
    record += ["--condition", f"{name}={batch_id}"]
if allow_drift:
    record.append("--allow-drift")
print("regenerating from", retained)
code = main(record)
if code:
    sys.exit(code)

ignored = {"finished_at", "report_path"}
if allow_drift:
    ignored |= {"frozen_set_verified", "frozen_set_drifted", "frozen_set_drift", "decision"}
fresh_dir = runs / "experiments" / kept["experiment_id"]
differs = False
for name, skip in (("result.json", ignored), ("repair_effectiveness.json", set())):
    before = json.loads((retained / name).read_text(encoding="utf-8"))
    after = json.loads((fresh_dir / name).read_text(encoding="utf-8"))
    before, after = ({k: v for k, v in d.items() if k not in skip} for d in (before, after))
    if before == after:
        left_out = f", leaving out {', '.join(sorted(skip))}" if skip else ""
        print(f"{name}: identical{left_out}")
        continue
    differs = True
    print(f"{name}: DIFFERS")
    lines = [json.dumps(d, indent=2, sort_keys=True).splitlines() for d in (before, after)]
    sys.stdout.writelines(
        line + "\n"
        for line in difflib.unified_diff(*lines, "retained", "regenerated", lineterm="")
    )
sys.exit(1 if differs else 0)
EOF
