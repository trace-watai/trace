# Experiment contract

An experiment is the record that says two batches are the two sides of one
question. A batch is one run of one suite under one or more agent
configurations; nothing in it relates it to any other batch. Run the suite with
a guardrail on and again with it off and you get two batch ids in the batches
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

`fixtures_hash` is what makes the freeze checkable rather than asserted. If the
fixtures move between two conditions then the conditions answered different
questions, and comparing them is void.

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
replay-validity question exists to measure, which is why it is a named kind
rather than an implementation detail.

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
to be made on the evidence rather than on an average of it.

Every metric is nullable, and a missing one stays null rather than becoming
zero. A condition set that never ran live cannot produce a divergence rate, and
reporting that as `0.0` would read as a measurement that was never taken. Today
`experiment record` derives `verified_failure_count`, `cost_usd` and
`latency_ms_p50` from the batch summaries; the rest arrive with the branch
stage (#159) and the post-block classifier.

## Commands

```bash
trace-harness experiment record <experiment.json> --condition <name>=<batch_id> ...
trace-harness list-experiments
```

`record` reads the plan, never writes it, computes what it can, and writes the
result and report beside it. `list-experiments` prints one line per experiment
and replaces any hand-kept spreadsheet of them.

## The retained baseline

`docs/acceptance/experiments/exp_000_baseline/` is one recorded experiment with
a single `static_replay` condition over `refund_bundles_v0`, kept in the
repository the way the acceptance runs are. It exists so the contract is
demonstrated on real artifacts before the branch stage lands, and so the
dashboard loader has something to read. Its `verified_failure_count` is 5,
which is the suite's five bundle failures.

## Out of scope here

Running any condition, which is #159. Planner or analyst agents. Any combined
score.
