# Future: API server

**Status: not started — deliberately.** Owner: Samrath. This doc pins the
contract and the start condition so the work is ready to pick up, without
a placeholder app skeleton cluttering the repo.

> **Backing service exists (TRA-20).** The read layer is implemented as a
> documented Python service — `trace_harness.run_reader.RunReader` — that returns
> the artifact schemas unchanged. The FastAPI app below stays deferred; when its
> start condition is met it becomes a thin wrapper over `RunReader`, not new
> read logic. Method ↔ endpoint map:
>
> | Endpoint | `RunReader` method | Returns |
> |---|---|---|
> | `GET /runs` | `list_runs()` | `list[RunSummary]` |
> | `GET /runs/{id}` | `get_run(id)` | `RunResult` |
> | `GET /runs/{id}/task` | `get_task(id)` | `TaskSpec` |
> | `GET /runs/{id}/trace` | `get_trace(id)` | `list[TraceEvent]` |
> | `GET /runs/{id}/verifier` | `get_verifier(id)` | `VerifierResult \| None` |
> | `GET /runs/{id}/attribution` | `get_attribution(id)` | `AttributionResult \| None` |
> | `GET /runs/{id}/bundle` | `get_bundle(id)` | `FailureBundle \| None` |
>
> **Missing-artifact states:** unknown run id → `RunNotFound`; a not-yet-produced
> downstream artifact (verifier/attribution/bundle) → `None`.
>
> **Example calls:**
> ```python
> from trace_harness.run_reader import RunReader
> reader = RunReader.from_runs_dir("runs")
> for s in reader.list_runs():        # cheap summaries for a run list
>     print(s.run_id, s.status, s.task_id)
> reader.get_run(run_id)              # RunResult (how it ended)
> reader.get_verifier(run_id)         # VerifierResult or None (not yet verified)
> reader.get_bundle(run_id)           # FailureBundle or None (not yet bundled)
> ```
> Or from the CLI: `trace-harness list-runs`.

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
