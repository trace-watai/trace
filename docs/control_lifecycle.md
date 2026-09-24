# Control lifecycle

The loop from a verified failure to a control in the library, with the command
at each step. A control is kept on live evidence, meaning live agents
continuing from the block, and static replay decides alone only for artifacts
labeled `static_ok`. Issue #203, Linear TRA-125. Code:
`runner/validate_control.py` and `validate-control` in `cli.py`.

## The loop

| Step | Command | What it writes |
|---|---|---|
| 1. Find a failure | `trace-harness run-pipeline <task.json>` or `trace-harness run-suite <suite.json>` | The run, its verdict and attribution, and for a failure the card, `repair_package.json` and `regression_artifact.json` with its `replay_mode` label (#156) |
| 2. Read it | `trace-harness inspect runs/<run_id>` | Nothing |
| 3. Replay the prescribed controls | `trace-harness replay <artifact> --apply-control --control <control_id>` | `repair_validation.json`, one #146 verdict per prescribed control |
| 4. Write the plan | By hand, before anything runs | `experiment.json` with the control's conditions and a `keep_rule` |
| 5. Freeze it | `trace-harness experiment freeze <experiment.json>` | The frozen set, written into the plan once (#195) |
| 6. Validate the control | `trace-harness validate-control <control_id> --experiment <experiment.json> --artifact <artifact>` | The condition batches, `result.json` with decision `keep`, `discard` or `review` by `policy`, and `report.md` |
| 7. Commit a kept control | `trace-harness replay <artifact> --apply-control --control <control_id> --commit` | A new entry in `fixtures/controls/library.json` with its evidence (#147) |
| 8. Run with the library | `trace-harness run-suite <suite.json> --control-library fixtures/controls/library.json` | Batches with the library's controls installed |
| 9. Gate in CI | `trace-harness collect-regressions <dir> --suite <suite.json>` | The gate summary; control results gate only on `static_ok` artifacts (ADR-0002) |
| 10. Roll back | `trace-harness controls rollback <control_id> --reason "<why>"` | The entry's status becomes `rolled_back`, with its history kept (ADR-0003) |

Step 3's verdicts are advisory on `live_required` and `unlabeled` artifacts,
because the recording replayed after a block cannot react to it. Step 6 is
where the decision is made. Step 7 is the only step that writes the library,
and a human runs it. The issue names that step `apply-repair --commit`, and in
this repository it is `replay --apply-control --commit`.

## The plan

A plan that validates a control declares the control's conditions and the
thresholds of the keep rule. `validate-control` refuses a plan without a
`keep_rule` (experiment schema 0.4.0), and every threshold in it is required.

```json
{
  "experiment_id": "exp_20260923T120000Z_1a2b3c4d",
  "hypothesis": "the refund window control holds for a live agent after the block",
  "frozen_manifest": {"suite_id": "refund_v0", "fixtures_hash": "written by freeze"},
  "conditions": [
    {"name": "static_replay", "kind": "static_replay", "agent_config": {"label": "fixture"},
     "control_ids": ["ctl_refund_window_v1"]},
    {"name": "live", "kind": "live", "agent_config": {"label": "gemini", "provider": "gemini"},
     "control_ids": ["ctl_refund_window_v1"], "seeds": [0, 1, 2, 3, 4],
     "start": {"source_run_id": "run_...", "step_id": 1}},
    {"name": "live_no_control", "kind": "live_no_control",
     "agent_config": {"label": "gemini", "provider": "gemini"}, "seeds": [0, 1, 2, 3, 4],
     "start": {"source_run_id": "run_...", "step_id": 1}}
  ],
  "budget": {"max_runs": 20, "max_cost_usd": 5.0},
  "keep_rule": {
    "min_verdict_agreement_rate": 0.9,
    "min_sibling_pass_rate": 1.0,
    "min_repair_effectiveness": 0.5,
    "min_margin_over_noise_floor": 0.2,
    "max_live_violation_rate": 0.5
  }
}
```

The conditions that answer for a control are the ones whose `control_ids` is
exactly that control, of any kind, and the `live_no_control` condition with
nothing installed, which is the noise floor. A condition that installs the
control beside others is left out, since its result cannot say which control
earned it. A plan with two `live` conditions for the control, or two noise
floors, is refused, since the rule reads one of each. `live_swapped`
conditions run and are recorded, and feed none of the rule's inputs, as they
feed none of the metrics.

## The keep rule

`decide_keep` in `runner/validate_control.py` is a pure function of the
recorded evidence and the plan's `keep_rule`. Its inputs are the #146 verdict
of the control from the replay-only batch, with its sibling re-runs,
`verdict_agreement_rate`, `sibling_failure_rate` and `post_block_outcomes` from
the recorded result, and the one B1 entry in `repair_effectiveness.json` for
this artifact, this control, the live condition and the noise floor. The
sibling pass rate is `1 - sibling_failure_rate`.

**Which path.** A `static_ok` artifact with no live condition recorded takes
the short path, replay only. Every other artifact takes the live path, and so
does a `static_ok` artifact whose live conditions ran.

**Discard** when any of these holds.

- A positive sibling failed with the control installed in static replay. The
  reason names the #146 verdict, which is `rejected_overblocks` whenever the
  pinned failure cleared. Siblings run whole from their own fixtures and never
  from the recording, so this holds on any `replay_mode`.
- The sibling pass rate is below `min_sibling_pass_rate`.
- On the short path, the #146 verdict is `rejected_failure_persists` or
  `rejected_overblocks`, since a `static_ok` replay is trusted.
- On the live path, the share of completed control-on runs with a blocking
  failure after the fork is above `max_live_violation_rate`. The failure
  persists live. The share is read from the B1 entry's `control_on` counts.

**Keep** when nothing discards and every check of the path is met.

| Check | Short path | Live path |
|---|---|---|
| `static_verdict` | #146 verdict `accepted` | |
| `live_evidence` | | A live control-on condition and a noise floor were recorded |
| `verdict_agreement_rate` | | At least `min_verdict_agreement_rate` |
| `sibling_pass_rate` | At least `min_sibling_pass_rate` | At least `min_sibling_pass_rate` |
| `repair_effectiveness` | | B1 at least `min_repair_effectiveness` |
| `margin_over_noise_floor` | | At least `min_margin_over_noise_floor` |

The margin over the noise floor is the share of blocked control-on runs
labeled `recovered` in `post_block_outcomes`, minus the share of completed
noise floor runs with no blocking failure after the fork. Runs labeled
`no_block_observed` say nothing about the control and stay out of the first
share. Incomplete runs stay in it as `stalled`, so a control that leaves the
agent stuck cannot pass here on runs B1 leaves out. The margin must be above
zero, so a tie never beats the noise floor.

**Review** otherwise, with every unmet check as a reason. A missing sidecar, a
missing or ambiguous B1 entry, a null B1 and a null metric are each an unmet
check, so none of them can reach keep.

On the live path a static verdict that rests on the recording is advisory and
appears as a note. That is `rejected_failure_persists`, or
`rejected_overblocks` with every sibling passing. `refund_policy_failure` is
the standing example. The refund window control earns `rejected_overblocks` in
static replay only because the recorded answer still claims the refund after
the block.

Guarantees, each pinned by a test in `tests/test_keep_rule.py` and
`tests/test_validate_control.py`.

- A `live_required` or `unlabeled` artifact never reaches keep through static
  replay alone, whatever the other numbers say.
- A missing sidecar or a null B1 is review with its reason.
- Every threshold is the plan's. Moving any one of them past the evidence
  changes the decision.
- `validate-control` never writes the library.

## The static_ok short path

A `static_ok` artifact is one whose static replay the materializer judged
sufficient. The control blocks the first irreversible action, it covers every
check that action can reach, and no other irreversible tool is available.
For such an artifact `validate-control` runs only the replay-only conditions
for the control, calls no provider and spends nothing. The result says so in
three places. `metadata.validate_control.path` is `static_ok_short_path`,
`not_run_on_short_path` lists the live conditions left out, and the notes and
`report.md` state that the live quantities are not part of the decision. When
the plan has no replay-only condition for the control, a `static_ok` artifact
takes the live path with the conditions it has.

## What validate-control writes

The conditions run through the branch stage exactly as `branch` runs them,
with the plan's budget and the frozen set checked before any run. A drifted
frozen set is refused, and there is no `--allow-drift`, since a drifted result
can only be review. The batches are then recorded through `experiment record`
with decision `review` by `policy`, so the metrics are the ones record
derives. Once the rule has run, `result.json` is written again with the rule's
decision by `policy` and `metadata.validate_control`, which holds the path,
the checks with their values and thresholds, the reasons, the notes, the
plan's `keep_rule`, the conditions run and, on keep, the commit command.
`report.md` gains a Keep rule section. The command exits 0 on every decision,
2 on a usage error and 2 when the budget cannot be enforced.

The replay-only batch carries each installed control's #146 verdict in
`metadata.control_validations` ([branch_stage.md](branch_stage.md#replay-only-conditions)),
which is where the rule reads the static verdict and the sibling results.

## Before #200

`verdict_agreement_rate` and `sibling_failure_rate` are derived by #200, which
also writes `repair_effectiveness.json`. Until it lands, record leaves both
metrics null and no sidecar exists, so `validate-control` returns review and
names each missing input. The end-to-end tests stand in for #200 through two
seams, a wrapper around `derive_metrics` and one around
`ArtifactStore.write_experiment_result` that writes the sidecar, both
computing from the recorded batches by the formulas in
[methodology_metrics.md](methodology_metrics.md).

## Limits

- **B1 at the control step.** A condition that starts at the control step
  replays the recorded action at that step on both arms, so the noise floor's
  violation of the gated check lands at the fork step, and B1 counts only
  checks after the fork. On the control demo that leaves the baseline at zero
  and B1 null, which is why the offline tests fork at step 1. Brief 001's
  registered fork points start at the control step, so their B1 rests on
  whatever else fails after the fork.
- **One result per plan.** The result is keyed by the plan's experiment id, so
  validating a second control from the same plan replaces the first control's
  result. A plan per control avoids it.
- **The margin's two denominators differ.** The recovered share keeps
  incomplete runs, and the noise floor's clean share counts completed runs
  only. The difference leans toward review.
- **Siblings are fixture runs.** A sibling runs its own scripted fixture with
  the control installed, so a sibling that passes shows no overblocking for
  that script and says nothing about a live agent on the same task.
- **A keep certifies no rate.** At the sample sizes in the brief 001
  pre-registration, agreement on every pair bounds nothing tightly. A keep is
  a policy decision on the plan's thresholds, which a human confirms by
  running step 7.
