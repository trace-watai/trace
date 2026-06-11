#!/usr/bin/env bash
# Repo health check: lint, format check, tests, and a pipeline smoke run
# into a throwaway runs dir. This is what CI should run; run it before
# pushing. Requires the package installed (see README quickstart).
set -euo pipefail
cd "$(dirname "$0")/.."

for tool in ruff pytest python; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "error: '$tool' not found on PATH." >&2
    echo "Activate the venv and install dev deps first (see README quickstart):" >&2
    echo "  source .venv/bin/activate && pip install -e \".[dev]\"" >&2
    exit 1
  fi
done

echo "==> ruff check"
ruff check .

echo "==> ruff format --check"
ruff format --check .

echo "==> pytest"
pytest

echo "==> pipeline smoke run (throwaway runs dir)"
SMOKE_DIR="$(mktemp -d)"
trap 'rm -rf "$SMOKE_DIR"' EXIT
python -m trace_harness.cli --runs-dir "$SMOKE_DIR" \
  run-pipeline fixtures/tasks/refund_policy_failure.json >/dev/null
python -m trace_harness.cli --runs-dir "$SMOKE_DIR" \
  run-pipeline fixtures/tasks/refund_policy_valid_cash.json --fail-on-verifier >/dev/null

echo "==> all checks passed"
