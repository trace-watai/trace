# Future: dashboard (trace replay & failure UX)

**Status: not started — deliberately.** Owner: Skye Haik. Data contracts:
coordinate with Samrath (trace/artifact schemas) and Darrel (attribution
semantics). This doc pins the requirements so the first PR can be real UI,
not scaffolding.

**Eventual stack:** Next.js + TypeScript + Tailwind + shadcn/ui, living in
`apps/dashboard/` once there is actual code.

**The non-negotiable starting point:** the first UI consumes **static JSON
from real runs** — generate one with
`trace-harness run-pipeline fixtures/tasks/refund_policy_failure.json`,
copy the `runs/{run_id}/` directory into the app's fixture data, and
render it. No backend, no live API. If a view can't be built from those
files, the gap is a data-contract conversation with Samrath, not a reason
to invent dashboard-side state.

**Required views (the failure fixture exercises every one):**

| View | Primary source | What it must show |
|---|---|---|
| Run summary | `run_result.json` + `task_spec.json` | status vs verdict distinction, steps, timing, task goal |
| Trace timeline | `trace.jsonl` | step-grouped events; prompts/actions/observations; retrieval results with doc **status badges** |
| Verifier failures | `verifier_result.json` | failed checks with expected/actual, severity, blocks_release, evidence drill-down to steps |
| Attribution | `attribution_result.json` | root cause vs missed recovery vs first irreversible — **distinct markers on the timeline** (steps 3 / 4 / 5 in the fixture), confidence + ambiguity notes |
| Failure card | `failure_card.json` | the human story: summary, blast radius, symptoms |
| Repair package | `repair_package.json` | controls with installation points + priorities |
| Regression artifact | `regression_artifact.json` | pinned scenario, checks, replay command, positive siblings |

**UX north star:** a teammate who wasn't there opens a failed run and
within a minute can say *what happened, where it became inevitable, and
what would prevent it*. Step ids are the cross-linking currency — every
evidence item carries them.

**Don't build yet:** auth, live polling, run comparison, editing. Render
one run excellently first.
