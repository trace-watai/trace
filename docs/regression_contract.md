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
| `schema_version` | `str` | auto | Currently `0.2.0`; bump when fields change |
| `test_name` | `str` | yes | Machine name: `regression_{task_id}` |
| `source_run_id` | `str` | yes | The run that produced this artifact |
| `task_fixture` | `str` | yes | Repo-relative path to the originating task JSON |
| `initial_state` | `dict` | yes | World state at run start, snapshotted from the recorded run |
| `pinned_docs` | `list[dict]` | no | Exact documents the agent saw, with status and metadata |
| `pinned_agent_actions` | `list[dict]` | no | The agent's recorded moves, in order; replay runs these instead of the fixture script. Empty on pre-`0.2.0` artifacts |
| `expected_behavior` | `list[str]` | no | What correct behavior looks like, from the task spec |
| `forbidden_actions` | `list[str]` | no | Actions the agent must never take, from the task spec |
| `verifier_checks` | `list[str]` | no | Check IDs that failed; these are the assertion set on replay |
| `positive_sibling_tests` | `list[SiblingTest]` | no | Companion scenarios that must keep passing |
| `severity` | `Severity` | yes | Highest severity from the verifier result |
| `blocks_release` | `bool` | yes | Whether a regression failure blocks the release gate |
| `replay_command` | `str` | yes | Shell command to reproduce the run |
| `metadata` | `dict` | no | Source verifier ID, `available_tools`, and `schema_versions` for every input this artifact depends on |

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

`trace-harness replay <regression_artifact.json>` has its own exit contract:

| Code | Meaning |
|---|---|
| `0` | Verifier reproduced the expected failed checks AND every positive sibling passed -- gate clear |
| `1` | Either the verifier didn't reproduce the expected checks, or a sibling failed -- gate fired |
| `2` | Usage/input error: bad artifact path, malformed JSON, missing fixture |

With `--apply-control` the meaning of `0`/`1` inverts for the pinned checks
only; see [Control-flip demo](#control-flip-demo---apply-control) below.
Fixture drift never affects the exit code.

### What CI should do with a regression artifact

For each `RegressionArtifact` where `blocks_release: true`:

1. Run `trace-harness replay <regression_artifact.json>` — rebuilds the run from the artifact's own pinned `initial_state`, `pinned_docs`, and `pinned_agent_actions` (the fixture named in `task_fixture` is read only for the tool subset and verifier ids), and asserts the verifier produces the expected `verifier_checks` as failed checks
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

`trace-harness replay` rebuilds the scenario from the artifact's own pinned
inputs, asserts the verifier produces the expected `verifier_checks`, and
exits 1 if it doesn't. Three things are pinned and used:

| Pinned input | Replaces |
|---|---|
| `initial_state` | the task fixture's `initial_state` |
| `pinned_docs` | the docs fixture the task points at |
| `pinned_agent_actions` | the script named in `metadata.fixture_script` |

So editing a task fixture, a docs fixture, or a script cannot silently change
what an existing regression asserts. That is the whole point of pinning them
at materialization time.

Two things are **not** pinned and still come from the task fixture:
`available_tools` and `verifier_ids`. Change those and you do change the
replay. The fixture must therefore still exist — a missing one is a usage
error (exit 2), not something replay works around.

Because replay no longer reads the fixture's own state or script, drift
between pinned and current would otherwise be invisible — so replay diffs
them and reports it:

```
⚠ fixture drift — orders[ORD-2026-0500] changed (purchase_age_days: 47 -> 30)
⚠ fixture drift — script length: 7 pinned action(s) -> 6 in the fixture
```

Drift is **informational and never changes the verdict** — the pinned inputs
are what ran. It means the fixture and the regressions built from it have
diverged, and someone should decide which is right: re-materialize the
regression from a fresh run if the fixture's new shape is intentional, or fix
the fixture if the drift was accidental. Action drift ignores each action's
`reasoning` text, so rewording a script's narration is not reported.

Positive siblings are named by path only — nothing about them is pinned — so
they always run from their live fixtures.

### Reproducibility metadata

`metadata.schema_versions` records the version of every input shape the
artifact depends on (`task`, `state`, `trace`, `verifier_result`, `harness`),
so a reader can tell whether a replay mismatch is a real regression or just a
schema that moved underneath it. There is deliberately no tool version — the
tool registry has no version concept — so `metadata.available_tools` records
the tool surface the run actually had instead.

### Control-flip demo (`--apply-control`)

```bash
trace-harness replay <regression_artifact.json> --apply-control
```

This installs the reference guardrails in `trace_harness/environment/guardrails.py`
on the environment before replaying, so a repair control can actually be
*demonstrated* flipping the gate, not just described in a repair package.
The assertion direction flips too:

- **Without** `--apply-control`: "gate clear" (exit 0) means the pinned
  failure still reproduces exactly as recorded. That's the normal meaning
  for a regression test — a known bug silently no longer reproducing
  usually means the fixture broke, not that someone fixed it.
- **With** `--apply-control`: "gate clear" (exit 0) requires *both* that
  every pinned check stopped firing **and** that the control introduced no
  new release-blocking check of its own. Both halves matter: a guardrail that
  blocks a harmful action while leaving the agent claiming it happened has
  relocated the failure, not removed it, and must not read as a clear gate.
  Positive siblings are re-run with the guardrail active either way, so
  `--apply-control` doubles as an overblocking check on legitimate behavior.

**Important limit, found while building this:** a guardrail can only change
what happens in *state* (did the tool call's side effect actually occur).
It cannot change what a scripted fixture agent says, because the fixture
adapter replays a fixed list of pre-authored actions and never reads a tool
result back into its next move — see `models/fixture.py`. Concretely:

- `fixtures/tasks/refund_policy_control_demo.json` is a minimal fixture
  built so `unauthorized_cash_refund` is the *only* possible violation — no
  ticket step, and a final answer that never claims the refund happened.
  Here, `--apply-control` flips the whole run FAIL → PASS by itself.
- `fixtures/tasks/refund_policy_failure.json` (the full 7-step staged
  failure) fails on 3 checks: `unauthorized_cash_refund`,
  `ticket_outage_claim_unsupported`, `deprecated_policy_treated_as_authoritative`.
  Blocking the refund with the same guardrail eliminates the first — but the
  script's ticket (step 6) and final answer (step 7) are hardcoded and
  unconditional, so blocking step 5 doesn't stop them from firing with the
  same content. Worse: it doesn't just leave the other two checks alone —
  it *introduces a new one*. The final answer ("I've issued a full cash
  refund...") was truthful in the uncontrolled run (the refund really did
  happen, so `final_answer_inconsistent_with_state` correctly stayed
  quiet); once the guardrail blocks the refund, that exact same hardcoded
  sentence becomes false, and `final_answer_inconsistent_with_state` fires
  where it didn't before. Net result after the control: still 3 failing
  checks, just a different set — the agent is now confidently lying about
  something that provably never happened. `replay --apply-control` on this
  fixture correctly exits 1 and names both problems separately (the pinned
  checks that still fire, and the blocking check the control introduced);
  that is not a bug in the guardrail or in `replay`, it's the fixture-anatomy
  limit this section is warning about.

This is a property of scripted-fixture testing, not of the guardrail
mechanism itself — a live model adapter would see the blocked tool call's
error and could choose not to write that ticket or claim, but fixture
scripts don't have a perception loop by design (deterministic, offline,
reproducible). Read both check results in a PR/demo write-up together; do
not just show the clean fixture and imply the guardrail fixes everything.

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

`fixtures/tasks/refund_policy_control_demo.json` is a separate, minimal
fixture built specifically for the `--apply-control` demo above — see that
section for why it needs to be a different fixture from the one above rather
than the same one with a flag.
