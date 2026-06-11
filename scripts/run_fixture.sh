#!/usr/bin/env bash
# Run the full TRACE pipeline on a task fixture (default: the staged refund
# failure). Extra arguments pass through to `trace-harness run-pipeline`.
#
#   scripts/run_fixture.sh
#   scripts/run_fixture.sh fixtures/tasks/refund_policy_valid_cash.json
#   scripts/run_fixture.sh fixtures/tasks/refund_policy_failure.json --fail-on-verifier
set -euo pipefail
cd "$(dirname "$0")/.."

TASK="${1:-fixtures/tasks/refund_policy_failure.json}"
shift || true

if command -v trace-harness >/dev/null 2>&1; then
  trace-harness run-pipeline "$TASK" "$@"
else
  python -m trace_harness.cli run-pipeline "$TASK" "$@"
fi
