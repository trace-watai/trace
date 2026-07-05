# Regression artifact and CI/release-gate contract

A verified failure becomes a `RegressionArtifact`: a machine-readable record
that pins everything needed to re-test the failure class later.

---

## What a regression artifact is

A `RegressionArtifact` is generated automatically by `FailureBundleGenerator`
as part of the `bundle` pipeline stage. It pins:

- The exact world the failure happened in (`initial_state`, `pinned_docs`).
  These are snapshots from the recorded run, not live fixtures that may change later.
- What the agent was supposed to do (`expected_behavior`, `forbidden_actions`)
- Which verifier checks failed and need to be asserted on replay (`verifier_checks`)
- At least one positive companion scenario that must keep passing
  (`positive_sibling_tests`).
- How to rerun it (`replay_command`)
- Whether it blocks a release (`blocks_release`, `severity`)

See `docs/examples/regression_artifact.example.json` for a concrete example
from the refund failure scenario.

---

## Field reference

| Field | Type | Required | Description |
|---|---|---|---|
| `schema_version` | `str` | auto | Currently `0.1.0`; bump when fields change |
| `test_name` | `str` | yes | Machine name: `regression_{task_id}` |
| `source_run_id` | `str` | yes | The run that produced this artifact |
| `task_fixture` | `str` | yes | Repo-relative path to the originating task JSON |
| `initial_state` | `dict` | yes | World state at run start, snapshotted from the recorded run |
| `pinned_docs` | `list[dict]` | no | Exact documents the agent saw, with status and metadata |
| `expected_behavior` | `list[str]` | no | What correct behavior looks like, from the task spec |
| `forbidden_actions` | `list[str]` | no | Actions the agent must never take, from the task spec |
| `verifier_checks` | `list[str]` | no | Check IDs that failed; these are the assertion set on replay |
| `positive_sibling_tests` | `list[SiblingTest]` | no | Companion scenarios that must keep passing |
| `severity` | `Severity` | yes | Highest severity from the verifier result |
| `blocks_release` | `bool` | yes | Whether a regression failure blocks the release gate |
| `replay_command` | `str` | yes | Shell command to reproduce the run |
| `metadata` | `dict` | no | Source verifier ID and honesty notes |

### `SiblingTest` fields

| Field | Type | Required | Description |
|---|---|---|---|
| `test_name` | `str` | yes | Human name for the sibling scenario |
| `task_fixture` | `str` | yes | Repo-relative path to the sibling task JSON |
| `description` | `str` | no | Why this sibling matters as an anti-overblocking check |

---

## CI/release-gate contract

### How the gate works

The CLI `verify` and `run-pipeline` commands support a `--fail-on-verifier`
flag:

```bash
trace-harness verify runs/<run_id> --fail-on-verifier
trace-harness run-pipeline fixtures/tasks/refund_policy_failure.json --fail-on-verifier
```

Exit codes:

| Code | Meaning |
|---|---|
| `0` | Verifier passed (or `--fail-on-verifier` not set) |
| `1` | Verifier failed AND `--fail-on-verifier` was set -- blocks release |

`check_repo.sh` already runs this gate as a smoke test on both the failure
fixture and the valid sibling.

### What CI should do with a regression artifact

For each `RegressionArtifact` where `blocks_release: true`:

1. Run `replay_command` (currently re-runs the originating fixture)
2. Assert the verifier produces the expected `verifier_checks` as failed checks
3. Run each `positive_sibling_tests[*].task_fixture` through the full pipeline
4. Assert every sibling produces a verifier PASS
5. Fail the CI run if any of the above break

Note: step 1 re-runs the fixture rather than replaying directly from the
pinned `initial_state`. A `trace-harness replay <regression_artifact.json>`
command that reads from pinned state is the next step (see TODO in
`regression/materializer.py`).

### Linear visibility guidance

- Set `blocks_release: true` on any regression artifact where the failure is
  `severity: critical` or `severity: high` and the check is release-blocking
- Link the regression artifact's `source_run_id` in the Linear ticket so the
  audit trail goes from ticket to run to artifact to CI gate
- When a regression gate fires in CI, open a Linear ticket in the owning
  workstream with the run directory linked and `priority: urgent`
- A regression that has been fixed and verified passing for two consecutive
  releases can be retired. File a ticket to remove it deliberately rather
  than letting it go stale and get ignored.

---

## Replay instructions

### Running a regression now (MVP)

```bash
# 1. Activate the environment
source .venv/bin/activate

# 2. Run the regression fixture through the full pipeline
trace-harness run-pipeline fixtures/tasks/refund_policy_failure.json --fail-on-verifier

# 3. Run the positive sibling (must pass)
trace-harness run-pipeline fixtures/tasks/refund_policy_valid_cash.json --fail-on-verifier
```

The `replay_command` field in each `RegressionArtifact` has the exact command
for that artifact. For the refund failure:

```
trace-harness run-pipeline fixtures/tasks/refund_policy_failure.json
```

### Running all repo health checks (including regression smoke)

```bash
scripts/check_repo.sh
```

This runs lint, format, tests, and the full pipeline smoke test on both
fixtures. Minimum bar before any PR.

### What "replay" will mean once fully implemented

A future `trace-harness replay <regression_artifact.json>` command will:

1. Load `initial_state` and `pinned_docs` directly from the artifact
2. Run the agent against that exact pinned world, not the live fixture
3. Assert the verifier result contains the artifact's `verifier_checks`
4. Run all `positive_sibling_tests` and assert each passes

This makes regression tests independent of fixture evolution. The seam for
this is in `regression/materializer.py`.

---

## Example artifacts

`docs/examples/regression_artifact.example.json` is the refund failure regression
with all fields populated from a real pipeline run. The sibling test points to
`fixtures/tasks/refund_policy_valid_cash.json`, which must produce a verifier
PASS in the same CI gate.
