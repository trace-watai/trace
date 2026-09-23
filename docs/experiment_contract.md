# Experiment contract

An experiment is the record that says two batches are the two sides of one
question. A batch is one run of one suite under one or more agent
configurations; nothing in it relates it to any other batch. Run the suite with
a guardrail on and again with it off and you get two batch ids in the batches
folder, with the comparison living in somebody's notes.

Owner: Evaluation Systems. Schema: `trace_harness/runner/experiment.py`
(`EXPERIMENT_SCHEMA_VERSION = 0.3.0`; 0.2.0 added the frozen set and 0.3.0
added `continuation_script`). Metric names come from
[methodology_metrics.md](methodology_metrics.md) and are asserted against its
appendix by `tests/test_experiment.py`.

## Two files

```
runs/experiments/{experiment_id}/
  experiment.json    the plan, written before anything runs
  result.json        which batch answered which condition, and what was decided
  report.md          the same thing for a human
```

**The plan comes first.** That ordering is the point. A plan composed after
seeing the numbers is not a plan, and the whole reason to write the hypothesis
and the frozen manifest down is that they cannot then be adjusted to fit the
result.

### `experiment.json`

| field | meaning |
|---|---|
| `experiment_id` | `exp_YYYYMMDDTHHMMSSZ_xxxxxxxx`, matching the batch id style |
| `brief_path` | the research brief this comes from, when there is one |
| `hypothesis` | one sentence, stated before running |
| `frozen_manifest` | `suite_id`, `verifier_ids`, `fixtures_hash`, `labels_path`, `frozen_set` |
| `conditions` | one per arm, names unique within the experiment |
| `budget` | `max_runs`, `max_cost_usd` |

`fixtures_hash` is what makes the freeze checkable rather than asserted. If the
fixtures move between two conditions then the conditions answered different
questions, and comparing them is void. `frozen_set` extends the freeze to the
whole evaluator; see [The frozen evaluator](#the-frozen-evaluator).

A condition declares its `kind`, the `agent_config` to run under, the
`control_ids` to install, the `seeds`, and where in a recorded run to `start`
if not from the beginning. `continuation_script` is optional and names a
fixture script the branch stage plays after the start step, for the fixture
provider only; without one the fixture provider plays the recording.

| kind | what it does |
|---|---|
| `static_replay` | re-runs recorded actions; cannot react to being blocked |
| `live` | a live agent with the declared controls installed |
| `live_no_control` | the same agent with nothing installed, the noise floor |
| `live_swapped` | the same conditions under a different model |

`static_replay` not being able to react to a block is the limitation the whole
replay-validity question exists to measure, which is why it is a named kind
rather than an implementation detail.

### `result.json`

`condition_batches` maps each declared condition to the batch that answered it.
Recording a batch under a name the plan never declared is a usage error: a
result describing different arms than its plan is not a result for that
experiment.

`decision` is one of `baseline`, `keep`, `discard`, `review`, and `decided_by`
records whether a `human` or a `policy` made the call.

`frozen_set_verified`, `frozen_set_drifted` and `frozen_set_drift` record how the
plan's frozen set compared with the tree at record time. Both flags false means
nothing was checked.

## The frozen evaluator

An experiment cannot change the evaluator that scores it. The plan's
`frozen_manifest.frozen_set` holds a sha256 for every file of the evaluator,
written once by `experiment freeze` before any condition runs. `experiment
record` recomputes them and refuses, listing each file that changed, was added
or was removed. The module is `trace_harness/runner/frozen_set.py`.

| component | what is hashed |
|---|---|
| `verifiers` | `src/trace_harness/verifiers/` |
| `environment` | `src/trace_harness/environment/` |
| `attribution` | `src/trace_harness/attribution/` |
| `suite` | `fixtures/suites/{suite_id}.json` |
| `fixtures` | `fixtures/` except `fixtures/controls/` |
| `labels` | the plan's `labels_path`, when it names one |

The attribution scorer is `src/trace_harness/attribution/`: `HeuristicAttributor`,
the `AttributionResult` schema it emits, and `validate_attribution_result`, which
it runs on its own output. No attribution accuracy scorer exists yet (C1 in
[methodology_metrics.md](methodology_metrics.md)); one added under that directory
is frozen with it.

`fixtures/controls/` is left out. The control library is the treatment an
experiment varies, brief 001 lists its registry entries as an allowed change,
and its evidence directories gain a generated `index.json` when re-verified.

**Hashing.** A file's hash is sha256 over its bytes with CRLF folded to LF,
keyed by its POSIX path relative to the repository root. A component's digest is
sha256 over its sorted `path, hash` lines, so neither an autocrlf checkout nor
the order a filesystem lists a directory changes it. `__pycache__`, `.pyc`,
`.pyo`, tool caches and `.DS_Store` are skipped; every other byte counts,
comments and docstrings included. Paths resolve against the working directory
like every other CLI path, so `freeze` and `record` run from the repository
root. `freeze` sets `fixtures_hash` to the fixtures digest, and a plan whose two
values disagree fails to load.

**A plan is frozen once.** `freeze` refuses a plan that already carries a frozen
set. Freezing it again after the evaluator moved would turn drift into a clean
record, so a changed evaluator needs a new plan.

**Recording.** When nothing differs the result carries `frozen_set_verified:
true`. When anything differs `record` exits 2 and writes nothing:

```
error: the frozen set of exp_20260923T120000Z_1a2b3c4d changed since the plan was frozen, so recording is refused:
  verifiers: changed src/trace_harness/verifiers/refund_policy.py
Restore those files, or pass --allow-drift to record the result as drifted with decision review.
```

With `--allow-drift` the result is written with `frozen_set_drifted: true`, the
files in `frozen_set_drift`, and decision `review` whatever `--decision` said.
`ExperimentResult` rejects a drifted result with any other decision, so editing
`result.json` by hand cannot turn it back into a keep. With nothing drifted,
`--allow-drift` changes nothing.

**Plans from 0.1.0.** A plan written under schema 0.1.0 has no frozen set and
still loads. `record` proceeds, and the result carries both flags false, which
reads as unchecked and never as verified. A plan at 0.2.0 or later without a
frozen set is refused with a pointer to `experiment freeze`.

**Retained experiments in CI.** `check_repo.sh` passes `--experiments
docs/acceptance/experiments` to `collect-regressions`, which recomputes every
retained experiment's frozen set. One that fails to load fails the gate with
exit 2. Drift prints a warning and lands in the gate summary's `experiments`
and `experiments_drifted`, and never changes the exit code. A retained
experiment was checked when it was recorded; a later reviewed edit to the
verifier makes it stale without making its recorded numbers wrong. Blocking
would turn CI red on every verifier change until each retained baseline was
re-run, tying unrelated work to re-baselining. The cost is that CI never forces
a stale baseline to be re-run, and the warning is the only prompt to do it.

**Limits.** The check compares plan time with record time. A batch produced
before the plan was frozen, or on another checkout, goes unnoticed, because a
batch summary carries no frozen-set digest. Model weights and provider behavior
are out of scope; cassettes cover them.

## The metrics

Eight, named in the memo, with no combined score. A single number would let a
good result on one axis hide a bad one on another, and the decision is supposed
to be made on the evidence rather than on an average of it.

Every metric is nullable, and a missing one stays null rather than becoming
zero. A condition set that never ran live cannot produce a divergence rate, and
reporting that as `0.0` would read as a measurement that was never taken.
`experiment record` derives `verified_failure_count`, `cost_usd` and
`latency_ms_p50` from any batch summaries, and the two divergence rates and
`post_block_outcomes` from the batches the branch stage writes, with the
counts behind each rate in `extra` ([branch_stage.md](branch_stage.md#metrics)).
`verdict_agreement_rate` and `sibling_failure_rate` are not derived yet.

## Commands

```bash
trace-harness experiment freeze <experiment.json>
trace-harness experiment record <experiment.json> --condition <name>=<batch_id> ... [--allow-drift]
trace-harness list-experiments
```

`freeze` writes the frozen set into the plan and is the only command that
writes a plan. `record` reads the plan, never writes it, checks the frozen set,
computes what it can, and writes the result and report beside it.
`list-experiments` prints one line per experiment and replaces any hand-kept
spreadsheet of them.

## The retained baseline

`docs/acceptance/experiments/exp_000_baseline/` is one recorded experiment with
a single `static_replay` condition over `refund_bundles_v0`, kept in the
repository the way the acceptance runs are. It exists so the contract is
demonstrated on real artifacts before the branch stage lands, and so the
dashboard loader has something to read. Its `verified_failure_count` is 5,
which is the suite's five bundle failures.

Its plan stays at schema 0.1.0 with no frozen set. Its only batch ran on
2026-09-17, and the verifiers, environment, attribution and fixtures have all
changed since (#188, #190, #192, #193). Freezing the plan against today's tree
would certify a batch that today's evaluator did not produce, which is the false
reading the frozen set exists to prevent. It records as unchecked, and the
collector lists it as `not_recorded`. Re-baselining it takes a fresh batch and
a newly frozen plan.

## Out of scope here

Running a condition, which is the branch stage
([branch_stage.md](branch_stage.md)). Planner or analyst agents. Any combined
score.
