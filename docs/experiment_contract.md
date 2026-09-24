# Experiment contract

An experiment is the record that says two batches are the two sides of one
question. A batch is one run of one suite under one or more agent
configurations; nothing in it relates it to any other batch. Running the suite
with a guardrail on and again with it off leaves two batch ids in the batches
folder, with the comparison living in somebody's notes.

Owner: Evaluation Systems. Schema: `trace_harness/runner/experiment.py`
(`EXPERIMENT_SCHEMA_VERSION = 0.1.0`). Metric names come from
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
| `experiment_id` | `exp_YYYYMMDDTHHMMSSZ_xxxxxxxx` when generated, matching the batch id style; any id must be letters, digits, `_` and `-`, since it names a directory |
| `brief_path` | the research brief this comes from, when there is one |
| `hypothesis` | one sentence, stated before running |
| `frozen_manifest` | `suite_id`, `verifier_ids`, `fixtures_hash` |
| `conditions` | one per arm, names unique within the experiment |
| `budget` | `max_runs`, `max_cost_usd` |

`experiment_id` and `created_at` default when a plan is built in code, but a
plan file must state both. A defaulted id would file every record of the same
file as a new experiment.

`suite_id` is enforced: `experiment record` refuses a batch whose summary names
another suite. `fixtures_hash` is stored exactly as the plan states it, and
nothing on this branch computes it from the fixture files or compares it with
them. It records what the author froze and proves nothing about the files. The
retained baseline's `sha256:01e4172931eda28c` was entered by hand. Computing
the hash, and refusing a record when the files moved, arrives with
`experiment freeze` (#195).

A condition declares its `kind`, the `agent_config` to run under, the
`control_ids` to install, the `seeds`, and where in a recorded run to `start`
if not from the beginning.

Each control id is checked when the plan loads. It must be an id
`select_controls` accepts, the lookup `replay --control` uses and the branch
stage (#159) installs through, and its guardrail must resolve in the
guardrail registry with the rules its `rule_ref` names. A misspelled id therefore fails before any
condition runs. Every entry in `fixtures/controls/library.json` was committed
from those controls, and a test keeps each of them loadable in a plan.

| kind | what it does |
|---|---|
| `static_replay` | re-runs recorded actions; cannot react to being blocked |
| `live` | a live agent with the declared controls installed |
| `live_no_control` | the same agent with nothing installed, the noise floor |
| `live_swapped` | the same conditions under a different model |

`static_replay` not being able to react to a block is the limitation the whole
replay-validity question exists to measure, which is why it is a named kind of
its own.

### `result.json`

`condition_batches` maps each declared condition to the batch that answered it.
Recording a batch under a name the plan never declared is a usage error: a
result describing different arms than its plan is not a result for that
experiment.

`decision` is one of `baseline`, `keep`, `discard`, `review`, and `decided_by`
records whether a `human` or a `policy` made the call.

## The metrics

Eight, named in the memo, with no combined score. A single number would let a
good result on one axis hide a bad one on another, and the decision is supposed
to be made on the evidence itself.

Every metric is nullable, and a missing one stays null. A condition set that
never ran live cannot produce a divergence rate, and reporting that as `0.0`
would read as a measurement that was never taken.

Today `experiment record` derives three of the eight from the batch summaries,
following Part B2 of [methodology_metrics.md](methodology_metrics.md):

| metric | how | null when |
|---|---|---|
| `verified_failure_count` | completed runs whose verdict is `fail` | no completed run carries a verdict, including when no `--condition` was given |
| `cost_usd` | sum of the costs that were recorded; `extra` carries `cost_recorded_k` (runs with a cost) and `cost_recorded_n` (runs) | no run recorded a cost |
| `latency_ms_p50` | median latency over completed runs | no completed run recorded one |

All three pool every recorded condition. When more than one condition is
recorded, `extra` also carries `verified_failure_count.<condition>` and
`latency_ms_p50.<condition>`, so a fixture arm's near-zero latency cannot hide
inside a live arm's median. A batch may answer only one condition, since
pooling it twice would count its runs twice.

The batch entry does not record `blocks_release`, so a failure of a
non-blocking check counts toward `verified_failure_count` although B2 counts
blocking failures only. The divergence rates arrive with the branch stage
(#159) and `post_block_outcomes` with the post-block classifier (#157).
Nothing derives `verdict_agreement_rate` or `sibling_failure_rate` yet.

## Commands

```bash
trace-harness experiment record <experiment.json> --condition <name>=<batch_id> ...
trace-harness list-experiments
```

`record` reads the plan, computes what it can, and writes the result and
report under `runs/experiments/{experiment_id}/`. The first record also stores a
copy of the plan there, and no later record rewrites it. Recording again with a
plan that differs from the stored copy exits 2 and writes nothing, since
changing a plan after its numbers came in is what writing it first prevents; a
changed plan needs a new `experiment_id`. Recording the same plan again replaces
the result, which is how a decision is revised. Naming one condition twice, or
passing a batch from another suite, also exits 2 before anything is written.

`list-experiments` prints one line per experiment and replaces any hand-kept
spreadsheet of them. An experiment whose files do not load, including one whose
plan or result names a different experiment than its directory, gets an
`unreadable` line with its error on stderr, the others are still listed, and the
exit code is 1. `RunReader.list_experiments` and the dashboard's
`listExperiments` likewise leave such an experiment out, and
`RunReader.unreadable_experiments` and `listUnreadableExperiments` name it.

## The retained baseline

`docs/acceptance/experiments/exp_000_baseline/` is one recorded experiment with
a single `static_replay` condition over `refund_bundles_v0`, kept in the
repository the way the acceptance runs are. It exists so the contract is
demonstrated on real artifacts before the branch stage lands, and so the
dashboard loader has something to read. Its `verified_failure_count` is 5,
which is the suite's five bundle failures, and a test derives it again from
the retained batch summary.

Only the batch summary under `docs/acceptance/batches/` was retained. The nine
run directories it names were not, so `RunReader.get_run` and the dashboard
cannot open them. The summary carries every per-run field the three derived
metrics read, which is why it alone is enough to re-derive them. A fresh
`run-suite fixtures/suites/refund_bundles_v0.json` reproduces the five failures
under new run ids, and a test records exactly that. The result predates the
cost coverage counts in `extra`, so its `extra` is empty; the eight named
metrics match what `record` derives today. Neither the batch nor the
experiment is read by the metrics history, which skips both trees.

## Out of scope here

Running any condition, which is #159. Planner or analyst agents. Any combined
score.
