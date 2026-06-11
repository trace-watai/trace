# Future: API server

**Status: not started — deliberately.** Owner: Samrath. This doc pins the
contract and the start condition so the work is ready to pick up, without
a placeholder app skeleton cluttering the repo.

**What it will be:** a thin FastAPI app serving run artifacts to the
dashboard and CI. It reads the same `runs/` layout the CLI writes (defined
in `src/trace_harness/tracing/artifact_store.py`) — no private database
schema until local JSON demonstrably hurts.

**Expected first endpoints (serve the artifact files; don't reshape them):**

```
GET /runs                       → list run ids + run_result summaries
GET /runs/{run_id}              → run_result.json
GET /runs/{run_id}/task         → task_spec.json
GET /runs/{run_id}/trace        → trace.jsonl (streamed or paginated events)
GET /runs/{run_id}/verifier     → verifier_result.json
GET /runs/{run_id}/attribution  → attribution_result.json
GET /runs/{run_id}/bundle       → failure_card + repair_package + regression_artifact
```

**Ground rules:**

- Responses are the artifact schemas (`schema_version` and all). If the
  API wants a different shape, that's a schema conversation with the
  harness, not a silent transform here.
- No auth/multi-tenancy/cloud complexity in v1 — localhost developer tool
  first.

**Definition of ready to start:** the dashboard renders real run dirs from
static JSON, and someone (dashboard or CI) needs the same query twice.
When that day comes, create `apps/api/` with actual code — not before.
