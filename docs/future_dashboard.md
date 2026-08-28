# Future: dashboard (trace replay & failure UX)

**Status: live run reads landed for run summary + failure card; other views
pending.** Owner: Skye Haik. Data contracts: coordinate with Samrath
(trace/artifact schemas) and Darrel (attribution semantics).

**Current stack:** Next.js + TypeScript + Tailwind + shadcn/ui in
`apps/dashboard/`.

**Live data:** `/runs` lists stored runs and `/runs/{run_id}` renders the run
summary and failure card for a real `runs/{run_id}/` directory, read directly
off disk by `src/data/run-loader.ts` — a TypeScript mirror of
`trace_harness.run_reader.RunReader`'s method surface (`list_runs`, `get_run`,
`get_task`, `get_trace`, `get_verifier`, `get_attribution`, `get_bundle`),
built on `src/data/run-store.ts` (the filesystem layer) and the existing
per-artifact parsers in `src/types/*`. It honors the harness's
`TRACE_RUNS_DIR` convention (see `apps/dashboard/.env.local.example`) so both
can point at the same runs directory. There is still no backend or live API —
this reads the filesystem directly, same as the CLI does; see
docs/future_api.md for when a real API server becomes worth building.
Missing-artifact and malformed-JSON states are explicit (404 for unknown
run, an inline panel for a not-yet-bundled run or unparsable artifact) rather
than a crash. `src/data/refund-failure-fixture.ts` and its bundled fixture
JSON remain for contract tests only — they're no longer in the live app's
render path.

If a view cannot be built from the artifact files on disk, the gap is a
data-contract conversation with Samrath, not a reason to invent
dashboard-side state.

**Required views (the failure fixture exercises every one):**

| View | Status | Primary source | What it must show |
|---|---|---|---|
| Run summary | Live | `run_result.json` + `task_spec.json` | status vs verdict distinction and steps are visible; timing and task goal remain |
| Trace timeline | Pending | `trace.jsonl` | step-grouped events; prompts/actions/observations; retrieval results with doc **status badges** |
| Verifier failures | Pending | `verifier_result.json` | failed checks with expected/actual, severity, blocks_release, evidence drill-down to steps |
| Attribution | Pending | `attribution_result.json` | root cause vs missed recovery vs first irreversible — **distinct markers on the timeline** (steps 3 / 4 / 5 in the fixture), confidence + ambiguity notes |
| Failure card | Live | `failure_card.json` | the human story: summary, blast radius, symptoms |
| Repair package | Pending | `repair_package.json` | controls with installation points + priorities |
| Regression artifact | Pending | `regression_artifact.json` | pinned scenario, checks, replay command, positive siblings |

**UX north star:** a teammate who wasn't there opens a failed run and
within a minute can say *what happened, where it became inevitable, and
what would prevent it*. Step ids are the cross-linking currency — every
evidence item carries them.

**Still out of scope:** auth, live polling, run comparison, and editing. The
next integration step is building the pending views (trace timeline, verifier
failures, attribution, repair package) on the same `run-loader.ts` seam the
run summary and failure card already use.
