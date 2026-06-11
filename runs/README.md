# runs/

Run artifacts land here (configurable via `TRACE_RUNS_DIR`). **Everything
in this directory except this README is gitignored** — runs are
regenerable outputs, not source.

One run, one directory:

```
runs/{run_id}/
  task_spec.json            what was asked (snapshot; itself a valid task file)
  run_config.json           how it was run (provider, model, limits, seed)
  initial_state.json        the world before
  trace.jsonl               what happened — one TraceEvent per line
  final_state.json          the world after (actual side effects)
  run_result.json           how it ended (status ≠ correctness!)
  verifier_result.json      was it actually correct      (written by `verify`)
  attribution_result.json   where/why it failed          (written by `attribute`)
  failure_card.json         human-readable failure       (written by `bundle`)
  repair_package.json       prevention controls          (written by `bundle`)
  regression_artifact.json  rerunnable regression spec   (written by `bundle`)
```

Run ids are `run_<UTC timestamp>_<hex8>` — lexicographic order is
chronological order. Partial directories are valid: a failed run keeps
whatever it wrote. Caught failures record an `error` event and still
append the final state snapshot and `run_finished`; only a hard kill
leaves a trace truncated mid-stream.

**Generate the two reference runs:**

```bash
trace-harness run-pipeline fixtures/tasks/refund_policy_failure.json
trace-harness run-pipeline fixtures/tasks/refund_policy_valid_cash.json
```

**Consumers:** the future dashboard and API (`docs/future_dashboard.md`,
`docs/future_api.md`) read these files as their data source — the layout
is the contract, defined in `src/trace_harness/tracing/artifact_store.py`
(Samrath owns it).

Do not commit run directories; do not hand-edit artifacts (regenerate
instead — edited artifacts make debugging sessions lie to you).
