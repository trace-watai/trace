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

Brief 001 runs this sequence once per condition, with its harness check,
retention and offline regeneration around it, in
[runbook_001.md](experiments/runbook_001.md).

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
| provider `external` (an outside agent, #210) | Not supported yet. The condition is refused before any run, since an outside agent runs its own loop from the task prompt and cannot take over a recorded run partway |

A live condition whose cassette mode is `replay` runs offline. When any of its
seeds has no cassette file, the whole condition is skipped: no runs, no batch,
a `skipped:` line, and exit code 0. A partial set of seeds is never run, so a
missing recording cannot quietly shrink the sample. Cassettes live at
`<directory>/<task_id>/<model>/<seed>.jsonl` and count steps from the first
call after the fork.

In `record` mode it is the other way round. Recording never overwrites a
cassette, so when any seed the condition could run, declared or replacement,
already has one, `branch` exits 2 before any run and lists them. A condition
is branched into its cassette folder once, and its first batch is the one to
record.

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
`metadata.replay_exit_code`. The positive siblings the replay ran go in
`metadata.siblings` as `test_name` and `run_id`, since the sibling rate reads
their verdicts. The divergence fields stay null.

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

`experiment record` maps each recorded batch to the condition it answers and
fills five metrics as Part B2 of
[methodology_metrics.md](methodology_metrics.md) defines them. The last two
read each run's `verifier_result.json` as well, because a blocking failure
after the fork depends on the steps of the failed checks, which a batch entry
does not keep (`runner/verdict_agreement.py`).

| Metric | Formula |
|---|---|
| `first_post_fork_divergence_rate` | `diverged / completed` over `live` batches |
| `noise_floor_divergence_rate` | The same over `live_no_control` batches |
| `post_block_outcomes` | Count per label over `live` batches |
| `verdict_agreement_rate` | Agreeing pairs over sufficient pairs of the `live` arm |
| `sibling_failure_rate` | Failed siblings over judged siblings, over `static_replay` batches with a control |

A pair is one arm, artifact, control and model. Its static verdict is clear
when `metadata.replay_exit_code` is 0 on the `static_replay` batch for that
artifact and control. A completed control-on seed is clear when its verdict
records no blocking failure after the fork, meaning no failed check with
`blocks_release` and a step id past the start step. The live verdict is clear
when at least half the completed seeds are. A pair with fewer than five
completed seeds, or with no static verdict, is excluded and its reason stated,
as pre-registration 001 requires. `live` and `live_swapped` pairs are rated per
model. The headline is the `live` arm's rate and is null when more than one
model answers that arm, since the pre-registration never pools models.

`extra` carries `verdict_agreement_k`, `_n` and `_excluded` for the headline,
the same three and `verdict_agreement_rate` per arm and model under
`/<arm>/<model>`, and for each pair
`pair/<arm>/<model>/<task>/<control>/` with `completed_seeds`,
`live_clear_share` and `static_clear`. The share is what the memo asks to see
beside a majority. `result.metadata.verdict_agreement_pairs` holds the same
pairs as rows with the exclusion reasons, and `report.md` prints them. A
sibling that never completed stays out of the sibling denominator, and
`sibling_failure_k` and `_n` go in `extra`.

The k and n behind each rate go in `metrics.extra` as
`first_post_fork_divergence_k` and `_n`, and `noise_floor_divergence_k` and
`_n`. Outcome counts include runs that did not complete, since `stalled`
exists for them, and `no_block_observed` is its own key. `live_swapped`
batches feed none of the three, because the pre-registration reports each
live model separately. Recording a batch under a condition other than the one
its metadata names exits 2, since it would swap the two rates.

## Budget

The plan's `budget.max_cost_usd` caps what the experiment spends on live
calls, across every condition and seed and every `branch` invocation into one
runs dir, through the #196 `BudgetGuard` that `run-suite` uses. The guard is
built once per invocation and starts from what the experiment's earlier runs in
the runs dir spent, which `branch` prints when it is not zero. That is the
`spent_usd` of the experiment's batches, plus every live run tagged with the
experiment in its `run_config.json` that no batch lists, priced from its own
trace as a batch entry is. Such a run is left by an invocation that was
interrupted, since a batch is written when its condition ends, or by a seed
that failed after its run existed. Branching one condition at a time, or again
after an interruption, therefore spends the cap once in total. Runs and
batches of other experiments never count. The guard follows the `run-suite`
contract: it admits a run before it starts and is charged the run's recorded
cost after it finishes, and an unknown cost never counts as zero.

- Before any condition runs, the guard is asked once about each live
  condition. A live model with no price under the cap, or a cap of zero, stops
  it there, so no live run of the invocation starts.
- Each live seed is admitted before it starts and charged after. Once the
  recorded spend reaches the cap, every later live seed of the invocation is
  refused, in this condition and in the ones after it. The check is between
  runs, so the overshoot is at most one run.
- A live seed that finishes with no recorded cost stops the guard as
  `budget_unenforceable`.
- The stop outlasts the invocation. When an earlier batch of the experiment
  stopped as `budget_unenforceable`, or an unbatched live run of it has no
  recorded cost, the next invocation starts stopped, prints why, and refuses
  every live seed. An interrupted run that recorded no provider response yet
  has no recorded cost, so interrupting the first live call of a seed stops
  the cap this way. A call in flight when an invocation is interrupted is
  missing from its run's trace, so the spend can be short by that call.
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
`Record with` line, and 0 after an exhausted cap. An invocation that runs only
`static_replay` conditions never asks the guard and exits 0 even when an
earlier stop was carried over. `max_runs` is recorded in the plan and not
enforced.

## Seed replacement

A plan may list `replacement_seeds` in its `metadata`. A live seed whose run
exists and ends with any status other than `completed` is then replaced by the
next unused seed from that list, and a replacement that ends incomplete is
replaced in turn, until the list runs out. The decision reads run status alone,
never the verdict, which is pre-registration 001's rule for seeds 5 to 9. A
seed the budget refused is not replaced, and neither is a `setup_error`, a
seed that failed before its run existed. That is a harness problem, and a
replacement would only spend a live seed on the same failure. Replacement
seeds land in the same batch with their own seed numbers. With cassettes in
`replay` mode, only the declared seeds are checked up front, so a replacement
with no recording ends as a `setup_error`. The list rides in plan metadata
because `ConditionSpec` has no field for it, and a malformed list exits 2
before any run.

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
- `max_runs` is not enforced. The cap spans the runs and batches in one runs
  dir, so two runs dirs of one plan may each spend up to it.
