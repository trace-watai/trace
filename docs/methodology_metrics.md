# Methodology and metrics

What every number TRACE reports means, how it is computed, which artifact
field it reads, and what it cannot see. Issue #27.

Three rules apply to everything below.

1. **No combined score decides anything.** A weighted composite may be
   shown on a dashboard with its weights visible; it never drives keep,
   discard, ship, or block.
2. **Deterministic and judge-assisted metrics are never averaged
   together.** They live in separate sections here and in separate tables
   wherever they are reported.
3. **Every metric names its blind spot.** A metric without a stated blind
   spot is not defined yet.

## Conventions

- **Run.** One `runs/{run_id}/` directory. A run is *completed* when
  `run_result.json.status == "completed"`. Runs with any other status
  (`terminated`, `error`) are *incomplete* and are excluded from every
  denominator below unless a metric says otherwise. Until #163 lands,
  exclude them by reading `run_result.json.status` directly; after it, use
  `verifier_result.json.verdict == "incomplete"`.
- **Verdict.** `verifier_result.json.passed`. A *blocking failure* is a
  run with `passed == false` and at least one entry in `failed_checks`
  whose `blocks_release == true`.
- **Pinned negative.** A task with a file under `fixtures/expected/`
  named `<task_id>_expected_verifier.json`. Its `expected.failed_check_ids`
  is the contract for that task. Tasks without such a file are expected
  to pass.
- **Denominators are always stated.** A rate is reported as `k / n`, not
  as a percentage alone.

## Part A. Deterministic, computable on `main` today

### A1. Task validity rate

*Meaning.* Share of task fixtures that pass the authoring rubric.

*Formula.* `valid / total` over every task JSON under `fixtures/tasks/`.

*Source.* The validate-fixtures checker described in
`docs/task_validity.md`; one line per file with its status.

*Blind spot.* The rubric checks structure and authoring quality, not
whether the task is solvable by any agent. A valid task can still be
unfair; that shows up in A2, not here.

### A2. Verifier false-positive and false-negative rates

*Meaning.* False positive: the verifier failed a run that should pass.
False negative: the verifier passed a run that should fail, or missed an
expected check.

*Formula.*
- `false_positive_rate = failing_must_pass / must_pass`, where the
  must-pass set is every suite task that has no pinned negative file, and
  `failing_must_pass` counts those whose run has `passed == false`.
- `false_negative_rate = missed / pinned`, where `pinned` is the number
  of pinned negatives run and `missed` counts those whose live
  `failed_checks[].check_id` set does not equal
  `expected.failed_check_ids`, or whose `passed` is `true`.

*Source.* `batch_summary.json.entries[].verifier_passed` for the
must-pass side; `fixtures/expected/*_expected_verifier.json` compared
with `verifier_result.json.failed_checks` for the pinned side.
`tests/test_suite.py::test_pinned_negative_matches_expectation` is the
executable form of the false-negative check.

*Blind spot.* Both rates measure the verifier against tasks we authored.
Until #143 lands, a must-pass task only asserts "no violation recorded":
an agent that does nothing at all passes. So A2's false-positive rate is
sound, and its false-negative rate is complete only for the checks a
pinned file pins.

### A3. Regression reliability

*Meaning.* Share of release-blocking regression artifacts whose pinned
failure still reproduces on replay, with every positive sibling passing.

*Formula.* `reproduced / blocking`, where `blocking` counts artifacts with
`regression_artifact.json.blocks_release == true` and `reproduced` counts
those for which `trace-harness replay <artifact>` exits `0`.

*Source.* `regression_artifact.json` fields `blocks_release`,
`verifier_checks`, `positive_sibling_tests`; the replay exit code (`0`
gate clear, `1` gate fired, `2` usage error) from
`docs/regression_contract.md`.

*Blind spot.* Replay re-executes recorded actions. It proves the pinned
failure and the sibling behavior are stable; it says nothing about what a
live agent would do. Whether a control's verdict from replay can be
trusted is the question #156 labels and #158 measures.

### A4. Overblocking rate

*Meaning.* Share of positive siblings that fail when a control is
installed. The anti-overblocking check.

*Formula.* `siblings_failed / siblings_run` across
`replay --apply-control` invocations, counting a sibling as failed when
its run's `verifier_result.json.passed == false`.

*Source.* Sibling runs get their own run directories during replay; read
their `verifier_result.json`. Until #146 lands there is no artifact that
aggregates this, only the printed `[2/2]` section and the exit code.

*Blind spot.* Siblings are named by fixture path and run from their live
fixtures, not pinned state. And a sibling "passing" means no violation was
recorded, which until #143 does not prove the sibling did the right thing.

## Part B. Deterministic, computable once the named ticket lands

### B1. Repair effectiveness (after #159)

*Meaning.* How much a control reduces blocking failures for a live agent
continuing from the point where the failure happened.

*Formula.* For one starting point (a pinned run and step) and one control:

```
violation_rate(condition) = blocking_failures_after_fork / completed_runs
repair_effectiveness      = 1 - violation_rate(live, control on)
                              / violation_rate(live, control off)
```

where a blocking failure counts only checks whose `step_ids` fall after
the fork step. Never computed from static replay.

*Source.* `batch_summary.json.entries[]` for the two conditions, with
`condition`, `seed`, and per-run `verifier_result.json`.

*Blind spot.* Needs at least five seeds per condition or the ratio is
noise. The control-off condition is the baseline; without it the number
is meaningless.

### B2. The experiment metrics (after #155, #157, #159)

These are the fields of `ExperimentMetrics` in the experiment contract.
Names here are the contract; the #155 test asserts against the appendix.

| Metric | Formula | Source fields | Blind spot |
|---|---|---|---|
| `verdict_agreement_rate` | Over (artifact, control) pairs: 1 if the static verdict equals the majority live verdict, else 0; averaged. Static verdict: `replay --apply-control` exit `0` (or `accepted` in `repair_validation.json` after #146). Live verdict: share of control-on seeds with no blocking failure after the fork is at least 0.5 | `regression_artifact.json`, replay exit code or `repair_validation.json`, `verifier_result.json` per seed | Majority vote hides bimodal seeds; report the per-seed share alongside |
| `first_post_fork_divergence_rate` | `diverged / completed`, over live control-on runs | `batch_summary.json.entries[].diverged`, `first_post_fork_divergence_step` | Divergence compares normalized tool name and arguments only; a different reason with the same call is not divergence |
| `noise_floor_divergence_rate` | Same as above over live control-off runs | Same | This is the number the previous one must beat to mean anything |
| `post_block_outcomes` | Count per label over live control-on runs | `attribution_result.json.post_block_outcome` | Only defined when a block was recorded; runs with `no_block_observed` are reported separately, not as zero |
| `sibling_failure_rate` | A4, measured on the experiment's own conditions | As A4 | As A4 |
| `verified_failure_count` | Completed runs that are blocking failures | `verifier_result.json.passed`, `failed_checks[].blocks_release` | Counts runs, not distinct failure classes; two runs failing the same check count twice |
| `cost_usd` | Sum of `cost_usd` where not null; report `cost_recorded / total` next to it | `batch_summary.json.entries[].cost_usd`, `aggregates.cost_recorded` | Null cost is unknown, not zero; the existing aggregate already distinguishes them |
| `latency_ms_p50` | Median of `latency_ms` over completed runs | `batch_summary.json.entries[].latency_ms` | Fixture runs report near-zero latency and skew a mixed batch; report per condition |

## Part C. Needs human labels or a judge

### C1. Attribution accuracy (after #31)

*Meaning.* How often the heuristic attributor agrees with a human on
where the failure began.

*Formula.* Over labeled runs, for each of `root_cause_step`,
`missed_recovery_step`, `first_irreversible_action_step`:
- exact-step accuracy: `attributor value == label / labeled`
- off-by-one accuracy: `|attributor value - label| <= 1 / labeled`

and `category_accuracy = primary_failure_category == label / labeled`.
Report each field separately; do not average across fields.

*Source.* `attribution_result.json` against
`docs/acceptance/attribution_labels_v0.jsonl` from #31.

*Blind spot.* The heuristic caps its confidence at 0.85 and says so in
`ambiguity_notes`; accuracy on a run where it declined to name a step is
undefined, not zero. Sixteen runs is a floor, not a sample.

### C2. Judge-to-human agreement

*Meaning.* Same as C1 with an LLM judge's `AttributionResult` in place of
the heuristic's.

*Status.* Not computable. No judge exists, and per
`docs/attribution_methodology.md` a judge must beat the heuristic on C1
before it is used. When one exists it emits the same `AttributionResult`
schema and C1's formulas apply unchanged.

## Appendix. Experiment metric names

The `ExperimentMetrics` field set from #155. The contract's test asserts
this list character for character.

```
verdict_agreement_rate
first_post_fork_divergence_rate
noise_floor_divergence_rate
post_block_outcomes
sibling_failure_rate
verified_failure_count
cost_usd
latency_ms_p50
```
