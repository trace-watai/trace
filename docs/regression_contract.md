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

1. Run `trace-harness replay <regression_artifact.json>` — loads pinned state and docs from the artifact, runs the agent, and asserts the verifier produces the expected `verifier_checks` as failed checks
2. Run each `positive_sibling_tests[*].task_fixture` through the full pipeline
3. Assert every sibling produces a verifier PASS
4. Fail the CI run if any of the above break

Severity and `blocks_release` are read directly off the `VerifierResult` — they come from the canonical `SEVERITY_MAP` in `verifiers/severity_map.py` and must not be recalculated in CI.

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

### Running a single regression

```bash
source .venv/bin/activate

# Replay the failure — must produce the expected failed checks
trace-harness replay runs/<run_id>/regression_artifact.json

# Run the positive sibling — must pass
trace-harness run-pipeline fixtures/tasks/refund_policy_valid_cash.json --fail-on-verifier
```

`trace-harness replay` loads `initial_state` and `pinned_docs` directly from
the artifact, runs the agent against that exact pinned world, asserts the
verifier produces the expected `verifier_checks`, and exits 1 if it doesn't.
This makes regression tests independent of fixture evolution.

### Running all repo health checks (including regression smoke)

```bash
scripts/check_repo.sh
```

Runs lint, format, tests, and the full pipeline smoke test. Minimum bar before any PR.

---

## Example artifacts

`docs/examples/regression_artifact.example.json` is the refund failure regression
with all fields populated from a real pipeline run. The sibling test points to
`fixtures/tasks/refund_policy_valid_cash.json`, which must produce a verifier
PASS in the same CI gate.
