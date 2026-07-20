#!/usr/bin/env bash
# TRACE end-to-end demo: prove the full local loop on two canonical tasks.
#
#   scripts/demo.sh
#
# Runs the refund/support pipeline
#   task -> agent -> trace -> verifier -> attribution -> failure card
#        -> repair package -> regression artifact
# on both sides of the vertical slice:
#
#   1. staged FAILURE   (unauthorized cash refund) -> verifier FAILS, full
#      failure bundle (card + repair + regression) is generated.
#   2. positive SIBLING (valid cash refund)         -> verifier PASSES, proving
#      the checks don't overblock legitimate behavior.
#
# Deterministic and offline: fixture provider, no API keys, no network, no live
# side effects. Artifacts land under a demo runs dir, each keyed by run id.
#
# This is the command for demo / readiness checks (e.g. Sarp): one run,
# both outcomes, every artifact on disk.
set -euo pipefail
cd "$(dirname "$0")/.."

# Default to an isolated demo dir so a demo never clutters ./runs; override with
# TRACE_RUNS_DIR to point elsewhere.
RUNS_DIR="${TRACE_RUNS_DIR:-runs/demo}"

# Find an interpreter that has trace_harness installed. Prefer the repo's own
# .venv (bash launched from an activated PowerShell venv does NOT inherit its
# PATH, so relying on `python`/`trace-harness` being on PATH is unreliable on
# Windows); fall back to whatever python is on PATH.
find_python() {
  for cand in .venv/Scripts/python.exe .venv/bin/python; do
    [ -x "$cand" ] && { echo "$cand"; return 0; }
  done
  for cand in python python3; do
    command -v "$cand" >/dev/null 2>&1 && { echo "$cand"; return 0; }
  done
  return 1
}

PY="$(find_python)" || {
  echo "error: no Python with trace_harness found." >&2
  echo "Create the venv and install the package first:" >&2
  echo "  python -m venv .venv && .venv/Scripts/pip install -e \".[dev]\"" >&2
  exit 1
}

run_pipeline() {
  "$PY" -m trace_harness.cli --runs-dir "$RUNS_DIR" run-pipeline "$@"
}

echo "############################################################"
echo "# TRACE demo 1/2: staged FAILURE"
echo "#   expect: verifier FAIL + attribution + failure bundle"
echo "############################################################"
run_pipeline fixtures/tasks/refund_policy_failure.json

echo
echo "############################################################"
echo "# TRACE demo 2/2: positive sibling"
echo "#   expect: verifier PASS (no overblocking, no bundle)"
echo "############################################################"
run_pipeline fixtures/tasks/refund_policy_valid_cash.json

echo
echo "############################################################"
echo "# Demo complete. Artifacts (keyed by run id) under: $RUNS_DIR"
echo "# List them with:"
echo "#   trace-harness --runs-dir $RUNS_DIR list-runs"
echo "############################################################"
