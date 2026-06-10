# TRACE trace & run schemas (TRA-22)

Canonical, lossless contract for a single agent run. Consumed by the runner (TRA-7),
storage (TRA-8), verifiers (TRA-16), attribution/judge (TRA-10), and the dashboard
data contract (TRA-27). Format goals (per Notion → *Trace Schema & Data Formats*):
**boring, inspectable formats** — JSONL trajectory + Pydantic models + OpenTelemetry-inspired spans.

Schema version: `TRACE_SCHEMA_VERSION` in `common.py` (currently `0.1.0`), stamped into
`RunMetadata.schema_version` and exported with the JSON Schemas.

## Object model

| Model | File | Role |
|---|---|---|
| `RunMetadata` | `run.py` | one per run; ids, status, timestamps, schema version |
| `AgentConfig` | `run.py` | **provider-agnostic** agent description (`provider` is a free string — no SDK coupling) |
| `TaskSnapshot` | `run.py` | frozen TaskSpec used for the run *(authoritative spec: TRA-6)* |
| `StateSnapshot` | `run.py` | initial/final environment state contract |
| `TraceEvent` (union) | `events.py` | one trajectory step per JSONL line |
| `VerifierResult` | `verification.py` | deterministic pass/fail *(interface: TRA-16)* |
| `AttributionResult` | `verification.py` | judge/root-cause output *(schema: TRA-10; taxonomy: TRA-13)* |
| `TraceRun` | `trace.py` | aggregate of all of the above + JSONL helpers |

Cross-team contracts (`TaskSnapshot`, `VerifierResult`, `AttributionResult`) use
`extra="allow"` so the owning tickets can extend them without breaking traces.
Events use `extra="forbid"` so malformed steps fail loudly.

## Event types (`step_type` discriminator)

`message · reasoning · retrieval · tool_call · tool_observation · state_snapshot ·
verifier · attribution · final_answer · error`

## Ordering & identity convention

- **`step_id`** — 0-indexed integer, **strictly increasing by emission order**, unique and
  gapless (`0..n-1`). It is the *canonical ordering key*; ordering never depends on
  timestamps. Stable across reloads. (`TraceRun.validate_ordering()` enforces this.)
- **`parent_step_id`** — links a derived event to its origin, giving the trace its parent
  relationships. A `tool_observation` points at the `tool_call` it answers (also
  correlatable via `call_id`).

## Design rules (load-bearing)

- Tool **arguments logged before** execution (`ToolCallEvent`); **observations after**
  (`ToolObservationEvent`). Two distinct events — preserves partial runs if the tool fails.
- **Raw vs. summarized** observations/reasoning kept in separate fields.
- `timestamp`, `cost`, `latency_ms` are **optional but present from day one**.
- **Failed runs are the product** — everything needed to replay is captured even on failure.
- **Evidence-first**: `VerifierResult` decides pass/fail; `AttributionResult` only explains and
  never overrides it. `Evidence` is flat/typed so the dashboard renders it without custom parsing.

## JSON Schema export

```bash
cd backend
python -m app.schemas.export                 # writes backend/schemas_json/*.schema.json
python -m app.schemas.export /path/to/out     # custom output dir
```

## Storage note

Physical file layout (`runs/{run_id}/trace.jsonl`, `run_metadata.json`, ...) and the durable
writer/reader are **TRA-8**, not here. This module guarantees the schema and a lossless JSONL
round-trip for the trajectory.
