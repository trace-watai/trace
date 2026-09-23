# Branch stage

`trace-harness branch` continues a recorded run from a chosen step with a
different agent, once per experiment condition and seed, and writes one batch
per condition for `experiment record` to read. Issue #159, Linear TRA-97.
Code: `runner/branch.py` and `models/fork.py`.

```bash
trace-harness branch <regression_artifact.json> --experiment <experiment.json> [--condition <name>]
```

Every selected condition is checked before any of them runs: its control ids
against the registry, its start's source run against the artifact, and its
start step against the recording. A bad condition exits 2 with nothing
written. The command ends by printing the `--condition name=batch_id` pairs
that `experiment record` accepts.

## From plan to result

The plan is frozen before any condition runs, and both `branch` and `record`
check the freeze
([experiment_contract.md](experiment_contract.md#the-frozen-evaluator)). All
three commands run from the repository root, since they hash paths relative to
it.

```bash
trace-harness experiment freeze experiment.json
trace-harness branch <regression_artifact.json> --experiment experiment.json
trace-harness experiment record experiment.json \
  --condition live=<batch_id> --condition live_no_control=<batch_id>
```

`freeze` needs the plan's `suite_id` to name a file under `fixtures/suites/`.
A task in no suite, such as the control demo, is still frozen through the
fixtures component. `branch` and `record` both refuse a plan past schema 0.1.0
that was never frozen, and both exit 2 with the changed files listed when the
verifier, environment, attribution scorer, suite or fixtures moved after the
freeze. `branch` checks before any run, so a changed evaluator costs nothing.
`branch --allow-drift` runs anyway and prints the drift, and `record` then
needs the flag too, which forces the decision to review.

## What a live condition does

For each seed of a `live`, `live_no_control` or `live_swapped` condition:

1. Rebuilds the world from the artifact's pinned state and documents, as
   `replay` does. The task fixture supplies only the tool subset and the
   verifier ids.
2. Installs the condition's `control_ids` through `select_controls` and
   `install_control`, so every block carries `blocked_by` in the trace.
3. Runs `ForkAdapter(prefix, continuation, switch_at_step=start.step_id)`.
   The recording answers steps 1 through the start step and the condition's
   agent answers every step after it. The runner is unchanged.
4. Verifies, attributes and bundles on failure as `run_task_pipeline` does,
   then labels the run with `classify_post_block_outcome`, passed runs
   included ([failure_taxonomy.md](failure_taxonomy.md#post-block-outcome-labels)).
5. Compares the run's actions after the start step with the recording's.

The start step is the last recorded step. At brief 001's fork points it is the
control step, so the recorded action there is replayed, the control blocks it,
and the agent takes over after the block. A condition with no `start` hands
the agent the whole run from step 1.

| `agent_config` | Continuation |
|---|---|
| provider `fixture`, no `continuation_script` | The recorded actions after the start step |
| provider `fixture` with `continuation_script` | That script's actions, from the step after the start |
| A live provider | `create_model_adapter` with the condition's model, temperature, timeout, prompt version, cassette and call policy, and the seed. `run_config.json` records the call policy as `run_task_pipeline` does |

A live condition whose cassette mode is `replay` runs offline. When any of its
seeds has no cassette file, the whole condition is skipped: no runs, no batch,
a `skipped:` line, and exit code 0. A partial set of seeds is never run, so a
missing recording cannot quietly shrink the sample. Cassettes live at
`<directory>/<task_id>/<model>/<seed>.jsonl` and count steps from the first
call after the fork.

## Divergence

Both fields compare `model_action` payloads through `material_action` in
`regression/replay.py`, the normalization `describe_action_drift` uses: the
action kind, the tool call with its arguments, and the final answer text.
Reasoning and provider state never count.

- `first_post_fork_divergence_step` is the first step after the start step
  where the run's action differs from the recording's, including a step only
  one of them reached. It is null when the run matched the recording to the
  end.
- `diverged` records whether the first action after the start step differed,
  which is what `first_post_fork_divergence_rate` averages. It is null when
  the run took no action after the start step.

Final answers and free-text arguments such as a refund `reason` compare as
text, so a live model diverges on them almost always. The noise floor exists
to measure exactly that.

## Replay-only conditions

A `static_replay` condition reuses `replay --apply-control` with the
condition's controls, or a plain replay when it has none. The replayed scenario
run becomes a batch of one whose entry carries the scenario's verdict and
post-block outcome, and the replay's exit code goes in
`metadata.replay_exit_code`. The divergence fields stay null.

## Batches

One batch per condition, at `runs/batches/{batch_id}/batch_summary.json`,
schema 0.4.0. Its `metadata` holds `experiment_id`, `condition`,
`condition_kind`, `source_run_id` and `start`. Each entry gains `condition`,
`seed`, `first_post_fork_divergence_step`, `diverged` and
`post_block_outcome`, and each run's index entry is tagged with the batch id,
as `run-suite` does. Suite batches leave the new fields null, and summaries
written before 0.4.0 load with them null and empty metadata. The dashboard
mirror is `apps/dashboard/src/types/batch-summary.ts`.

## Metrics

`experiment record` maps each recorded batch to the kind of the condition it
answers and fills three metrics as Part B2 of
[methodology_metrics.md](methodology_metrics.md) defines them.

| Metric | Formula |
|---|---|
| `first_post_fork_divergence_rate` | `diverged / completed` over `live` batches |
| `noise_floor_divergence_rate` | The same over `live_no_control` batches |
| `post_block_outcomes` | Count per label over `live` batches |

The k and n behind each rate go in `metrics.extra` as
`first_post_fork_divergence_k` and `_n`, and `noise_floor_divergence_k` and
`_n`. Outcome counts include runs that did not complete, since `stalled`
exists for them, and `no_block_observed` is its own key. `live_swapped`
batches feed none of the three, because the pre-registration reports each
live model separately. Recording a batch under a condition other than the one
its metadata names exits 2, since it would swap the two rates.

## Budget

The plan's `budget.max_cost_usd` caps what one `branch` invocation spends on
live calls, across every selected condition and seed, through the #196
`BudgetGuard` that `run-suite` uses. The guard is built once per invocation
and follows the same contract: it admits a run before it starts and is charged
the run's recorded cost after it finishes, and an unknown cost never counts as
zero.

- Before any condition runs, the guard is asked once about each live
  condition. A live model with no price under the cap, or a cap of zero, stops
  it there, so no live run of the invocation starts.
- Each live seed is admitted before it starts and charged after. Once the
  recorded spend reaches the cap, every later live seed of the invocation is
  refused, in this condition and in the ones after it. The check is between
  runs, so the overshoot is at most one run.
- A live seed that finishes with no recorded cost stops the guard as
  `budget_unenforceable`.
- A seed that calls no provider, meaning the fixture provider or a cassette
  replay, costs nothing and is never refused, even after the guard has
  stopped. `static_replay` conditions never ask the guard.

Every batch carries a `budget` block. Its `max_cost_usd` is the plan's cap and
its `spent_usd` is what that batch's live runs cost, so the blocks of one
invocation add up to what it spent. When the guard refused a seed of the
condition, or stopped while the condition ran, the block records
`budget_exhausted` or `budget_unenforceable` with the guard's detail and lists
each seed never run in `not_run`. A condition refused whole still writes its
batch, with no entries. `branch` exits as `run-suite` does without
`--fail-on-verifier`: 2 when the cap cannot be enforced, without the
`Record with` line, and 0 after an exhausted cap. `max_runs` is recorded in
the plan and not enforced.

## Harness check

Pre-registration 001 requires the fixture model on the `live` arm to equal
`static_replay` with zero divergence before any live run.
`tests/test_branch.py::test_fixture_live_arm_equals_static_replay_with_zero_divergence`
runs it from each registered fork point, `refund_policy_failure` at step 5 and
the day 31 and day 61 purchase-age tasks at steps 4 and 3, with
`ctl_refund_window_v1` installed and seeds 0 to 4. All three pass: the replay
exits 1, no seed is clear after the fork, and nothing diverges.

The two verdicts are defined differently. The static one counts pinned checks
at any step and the positive siblings, and the live one counts blocking
failures after the fork. Identical runs agree at the registered fork points
because nothing blocking fires at or before the fork and the siblings pass. A
fork point with a blocking check before the fork, or with a failing sibling,
would disagree without any harness defect.

## Limits

- A prefix recorded by the fixture adapter carries no provider state. No test
  here calls a live provider, so whether one accepts earlier turns without it
  is unexercised.
- `verdict_agreement_rate` and `sibling_failure_rate` stay null.
- `max_runs` is not enforced, and the cap is per invocation, so two
  invocations of one plan may each spend up to it.
