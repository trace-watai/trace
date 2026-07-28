# Future: dashboard (trace replay & failure UX)

**Status: static artifact foundation complete; full run view pending.** Owner:
Skye Haik. Data contracts: coordinate with Samrath (trace/artifact schemas) and
Darrel (attribution semantics).

**Current stack:** Next.js + TypeScript + Tailwind + shadcn/ui in
`apps/dashboard/`.

**Completed starting point:** the first UI consumes **static JSON from a real
run** — regenerate it with
`trace-harness run-pipeline fixtures/tasks/refund_policy_failure.json`,
via `python scripts/generate_sample_outputs.py`. The dashboard loads the full
11-artifact bundle through `src/data/refund-failure-fixture.ts`; its run summary
and failure card now share that source. There is still no backend or live API.
If a view cannot be built from those files, the gap is a data-contract
conversation with Samrath, not a reason to invent dashboard-side state.

**Required views (the failure fixture exercises every one):**

| View | Status | Primary source | What it must show |
|---|---|---|---|
| Run summary | Foundation complete | `run_result.json` + `task_spec.json` | status vs verdict distinction and steps are visible; timing and task goal remain |
| Trace timeline | Pending | `trace.jsonl` | step-grouped events; prompts/actions/observations; retrieval results with doc **status badges** |
| Verifier failures | Pending | `verifier_result.json` | failed checks with expected/actual, severity, blocks_release, evidence drill-down to steps |
| Attribution | Pending | `attribution_result.json` | root cause vs missed recovery vs first irreversible — **distinct markers on the timeline** (steps 3 / 4 / 5 in the fixture), confidence + ambiguity notes |
| Failure card | Complete for fixture | `failure_card.json` | the human story: summary, blast radius, symptoms |
| Repair package | Pending | `repair_package.json` | controls with installation points + priorities |
| Regression artifact | Pending | `regression_artifact.json` | pinned scenario, checks, replay command, positive siblings |

**UX north star:** a teammate who wasn't there opens a failed run and
within a minute can say *what happened, where it became inevitable, and
what would prevent it*. Step ids are the cross-linking currency — every
evidence item carries them.

**Still out of scope:** auth, live polling, run comparison, and editing. The
next integration step is selecting an arbitrary retained `runs/{run_id}`
directory and giving it explicit loading, missing-artifact, and malformed-data
states.
