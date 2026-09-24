# Experiment contract

An experiment is the record that says two batches are the two sides of one
question. A batch is one run of one suite under one or more agent
configurations; nothing in it relates it to any other batch. Running the suite
with a guardrail on and again with it off leaves two batch ids in the batches
folder, with the comparison living in somebody's notes.

Owner: Evaluation Systems. Schema: `trace_harness/runner/experiment.py`
(`EXPERIMENT_SCHEMA_VERSION = 0.3.0`; 0.2.0 added the frozen set and 0.3.0
added `continuation_script`). Metric names come from
[methodology_metrics.md](methodology_metrics.md) and are asserted against its
appendix by `tests/test_experiment.py`.

## Two files

```
runs/experiments/{experiment_id}/
  experiment.json    the plan, written before anything ran and copied here by the first record
  result.json        which batch answered which condition, and what was decided
  report.md          the same thing for a human
```

**The plan comes first.** That ordering is the point. A plan composed after
seeing the numbers can be fitted to them, and the whole reason to write the
hypothesis and the frozen manifest down is that they cannot then be adjusted to
fit the result.

### `experiment.json`

| field | meaning |
|---|---|
| `experiment_id` | `exp_YYYYMMDDTHHMMSSZ_xxxxxxxx` when generated, matching the batch id style; any id must be letters, digits, `_` and `-`, since it names a directory |
| `brief_path` | the research brief this comes from, when there is one |
| `hypothesis` | one sentence, stated before running |
| `frozen_manifest` | `suite_id`, `verifier_ids`, `fixtures_hash`, `labels_path`, `frozen_set` |
| `conditions` | one per arm, names unique within the experiment |
| `budget` | `max_runs`, `max_cost_usd`; `branch` enforces `max_cost_usd` per invocation ([branch_stage.md](branch_stage.md#budget)), and `max_runs` is not enforced |

`experiment_id` and `created_at` default when a plan is built in code, but a
plan file must state both. A defaulted id would file every record of the same
file as a new experiment.

`suite_id` is enforced: `experiment record` refuses a batch whose summary names
another suite. `fixtures_hash` makes the freeze checkable once the plan is
frozen, because `experiment freeze` sets it to the frozen set's fixtures digest
and `experiment record` recomputes that set. If the fixtures move between two
conditions then the conditions answered different questions, and comparing them
is void. `frozen_set` extends the freeze to the whole evaluator; see
[The frozen evaluator](#the-frozen-evaluator).

A plan without a frozen set, which only schema 0.1.0 allows at record time,
keeps `fixtures_hash` exactly as the plan states it. Nothing computes it from
the fixture files or compares it with them, so it records what the author froze
and proves nothing about the files. The retained baseline's
`sha256:01e4172931eda28c` was entered by hand.

A condition declares its `kind`, the `agent_config` to run under, the
`control_ids` to install, the `seeds`, and where in a recorded run to `start`
if not from the beginning. `continuation_script` is optional and names a
fixture script the branch stage plays after the start step, for the fixture
provider only; without one the fixture provider plays the recording.

Each control id is checked when the plan loads. It must be an id
`select_controls` accepts, the lookup `replay --control` uses and the branch
stage (#159) installs through, and its guardrail must resolve in the guardrail
registry with the rules its `rule_ref` names. A misspelled id therefore fails
before any condition runs. Every entry in `fixtures/controls/library.json` was
committed from those controls, and a test keeps each of them loadable in a
plan.

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

`frozen_set_verified`, `frozen_set_drifted` and `frozen_set_drift` record how the
plan's frozen set compared with the tree at record time. Both flags false means
nothing was checked.

## The frozen evaluator

A change to the evaluator after its plan is frozen is refused. The plan's
`frozen_manifest.frozen_set` holds a sha256 for every file of the evaluator,
written once by `experiment freeze` before any condition runs. `branch`
recomputes them before its first run and `experiment record` recomputes them
again, and each refuses, listing each file that changed, was added or was
removed. The module is `trace_harness/runner/frozen_set.py`.

| component | what is hashed |
|---|---|
| `verifiers` | `src/trace_harness/verifiers/` |
| `environment` | `src/trace_harness/environment/` |
| `attribution` | `src/trace_harness/attribution/` |
| `suite` | `fixtures/suites/{suite_id}.json` |
| `fixtures` | `fixtures/` except the generated `fixtures/controls/evidence/*/*/index.json` |
| `labels` | the plan's `labels_path`, one file under the repository root, when it names one |

The attribution scorer is `src/trace_harness/attribution/`: `HeuristicAttributor`,
the `AttributionResult` schema it emits, and `validate_attribution_result`, which
it runs on its own output. No attribution accuracy scorer exists yet (C1 in
[methodology_metrics.md](methodology_metrics.md)); one added under that directory
is frozen with it.

The control library in `fixtures/controls/` is frozen with the rest of
`fixtures/`. Brief 001 allows new control entries in `controls.py`,
`guardrails.py` and `library.json` only through an amendment made before the
first live run, and keeps existing entries as registered. A plan is frozen
before any condition runs, so a library change between freeze and record is
drift, as a change to `controls.py` or `guardrails.py` already is through the
`environment` component. The one exclusion is the `index.json` that
`ArtifactStore` writes beside retained evidence runs when they are re-verified,
at `fixtures/controls/evidence/*/*/index.json`. It is generated, `.gitignore`
carries the same pattern, and the library's sha256 pins do not cover it. The
pattern matches one path segment at a time, so an `index.json` at any other
depth counts.

`labels_path` has to be a relative POSIX path with no empty, `.` or `..`
segment, or the plan fails to load. `freeze` also refuses labels that name a
directory or resolve outside the repository root through a symlink.

**Hashing.** A file's hash is sha256 over its bytes with CRLF folded to LF,
keyed by its POSIX path relative to the repository root. A component's digest is
sha256 over its sorted `path, hash` lines, so neither an autocrlf checkout nor
the order a filesystem lists a directory changes it. `__pycache__`, `.pyc`,
`.pyo`, tool caches and `.DS_Store` are skipped; every other byte counts,
comments, docstrings and editor swap files included. A symlink anywhere in a
component is refused, because `os.walk` does not descend a linked directory and
its files would drop out of the hash. Paths resolve against the working
directory like every other CLI path, so `freeze` and `record` run from the
repository root, and a working directory that holds none of the three code
directories is refused, since hashing it would list every frozen file as
removed. `freeze` sets `fixtures_hash` to the fixtures digest, and a plan whose
two values disagree fails to load, as does a component whose digest does not
match its per-file hashes.

**A plan is frozen once.** `freeze` refuses a plan that already carries a frozen
set. Freezing it again after the evaluator moved would turn drift into a clean
record, so a changed evaluator needs a new plan. `freeze` reads only the plan it
is given, so a plan whose `frozen_set` was deleted by hand freezes again.

**Recording.** When nothing differs the result carries `frozen_set_verified:
true`. When anything differs `record` exits 2 and writes nothing:

```
error: the frozen set of exp_20260923T120000Z_1a2b3c4d changed since the plan was frozen, so recording is refused:
  verifiers: changed src/trace_harness/verifiers/refund_policy.py
Restore those files, or pass --allow-drift to record the result as drifted with decision review.
```

With `--allow-drift` the result is written with `frozen_set_drifted: true`, the
files in `frozen_set_drift`, and decision `review` whatever `--decision` said.
`ExperimentResult` refuses to load a result marked drifted with any other
decision, which stops an edit of the decision alone. With nothing drifted,
`--allow-drift` changes nothing.

**Plans from 0.1.0.** A plan written under schema 0.1.0 has no frozen set and
still loads. `record` proceeds, and the result carries both flags false, which
reads as unchecked and never as verified. A plan at 0.2.0 or later without a
frozen set is refused with a pointer to `experiment freeze`.

**Retained experiments in CI.** `check_repo.sh` passes `--experiments
docs/acceptance/experiments` to `collect-regressions`, which recomputes every
retained experiment's frozen set. One that fails to load, whose frozen set
cannot be hashed, or whose plan and result `record` could not have written
together is malformed and fails the gate with exit 2. Those pairs are a result
that claims a frozen-set check, verified or drifted, beside a plan with no
frozen set; a frozen plan beside a result with both flags false; and a plan
after schema 0.1.0 with no frozen set beside any result. A plan with no result
yet is a registration waiting for its runs and is listed as `not_recorded`.
Drift prints a warning and lands in the gate summary's `experiments` and
`experiments_drifted`, and never changes the exit code. A retained
experiment was checked when it was recorded; a later reviewed edit to the
verifier makes it stale without making its recorded numbers wrong. Blocking
would turn CI red on every verifier change until each retained baseline was
re-run, tying unrelated work to re-baselining. The cost is that CI never forces
a stale baseline to be re-run, and the warning is the only prompt to do it.

### Limits

The frozen set guarantees that on the checkout where `record` runs, a change to
any frozen file between `freeze` and `record` refuses the record, or with
`--allow-drift` records it as drifted with decision `review`. In CI, a retained
plan and result that contradict each other about the frozen set fail the gate.

It does not protect the plan or the result from being edited. Both are plain
JSON that nothing signs, so an edit that keeps the two files consistent loads
and passes the gate. Three such edits are

- clearing `frozen_set_drifted` and `frozen_set_drift` in `result.json`,
  setting `frozen_set_verified` and changing the decision to `keep`;
- deleting the plan's `frozen_set` and running `freeze` again against the
  changed tree;
- setting the plan back to schema 0.1.0 without a frozen set, so it records as
  unchecked.

Review of the plan's and the result's git history is what catches these.

The check also compares plan time with record time only. A batch produced
before the plan was frozen, or on another checkout, goes unnoticed, because a
batch summary carries no frozen-set digest. Model weights and provider behavior
are out of scope; cassettes cover them.

## The metrics

Eight, named in the memo, with no combined score. A single number would let a
good result on one axis hide a bad one on another, and the decision is supposed
to be made on the evidence itself.

Every metric is nullable, and a missing one stays null. A condition set that
never ran live cannot produce a divergence rate, and reporting that as `0.0`
would read as a measurement that was never taken.

`experiment record` derives three of the eight from any batch summaries,
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
blocking failures only.

The two divergence rates and `post_block_outcomes` come from the batches the
branch stage writes, with the counts behind each rate in `extra`
([branch_stage.md](branch_stage.md#metrics)). Nothing derives
`verdict_agreement_rate` or `sibling_failure_rate` yet.

## Commands

```bash
trace-harness experiment freeze <experiment.json>
trace-harness branch <regression_artifact.json> --experiment <experiment.json> [--allow-drift]
trace-harness experiment record <experiment.json> --condition <name>=<batch_id> ... [--allow-drift]
trace-harness list-experiments
```

`freeze` writes the frozen set into the plan and is the only command that
writes a plan file. `branch` runs the conditions and prints the pairs `record`
takes ([branch_stage.md](branch_stage.md)). `record` reads the plan, never
writes that file, checks the frozen set, computes what it can, and writes the
result and report under `runs/experiments/{experiment_id}/`. The first record
also stores a copy of the plan there, and no later record rewrites it.
Recording again with a plan that differs from the stored copy exits 2 and
writes nothing, since changing a plan after its numbers came in is what
writing it first prevents; a changed plan needs a new `experiment_id`.
Recording the same plan again replaces the result, which is how a decision is
revised. Naming one condition twice, passing a batch from another suite, or
passing a branch batch that ran for another experiment or condition, also
exits 2 before anything is written.

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
