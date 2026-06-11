# Trace schema

The trace is the evidence record of a run: an append-only sequence of
`TraceEvent` objects, one JSON object per line in
`runs/{run_id}/trace.jsonl`. Everything downstream — verifiers,
attribution, failure bundles, the dashboard — consumes traces. Owner:
Samrath. Schema: `trace_harness/tracing/events.py`
(`TRACE_SCHEMA_VERSION = 0.1.0`).

## Event envelope

```json
{
  "schema_version": "0.1.0",
  "event_id": "evt_000007",        // unique within the run, ordered
  "run_id": "run_20260611T025555Z_99032a0d",
  "step_id": 3,                    // decision step; null for run-level events
  "event_type": "model_action",
  "timestamp": "2026-06-11T02:55:55.123456+00:00",
  "payload": { ... },              // event-type-specific
  "metadata": { ... }              // free-form annotations
}
```

**Step numbering:** `step_id` counts agent decision steps from 1. All
events caused by one decision (prompt, action, tool call, observation)
share its id — this is the join key the entire system uses (verifier
evidence, attribution fields, dashboard timeline).

## Event types and payloads (MVP)

| event_type | step_id | payload (MVP) |
|---|---|---|
| `run_started` | null | task_id, provider, model, max_steps, timeout_seconds, prompt_version |
| `task_loaded` | null | full task spec |
| `state_snapshot` | null | `phase` ("initial"\|"final"), full state |
| `model_prompt` | step | transcript_length, `new_messages` (delta since last prompt — not the full transcript) |
| `model_action` | step | kind, reasoning?, tool_call?, final_answer? (normalized `AgentAction` minus `raw`) |
| `tool_call_requested` | step | tool_name, arguments |
| `tool_call_validated` | step | tool_name, valid, error? |
| `tool_call_executed` | step | tool_name, arguments, status, **side_effect**, error? — only emitted for valid calls |
| `tool_observation` | step | tool_name, status, result (full, incl. doc content), error? — what the agent saw |
| `retrieval_result` | step | query, result_count, results: [{doc_id, **status**, title, score, source}] (content lives in the observation) |
| `final_answer` | step | final_answer |
| `run_finished` | null | status, termination_reason, steps_taken |
| `error` | step? | error, kind (script_exhausted \| model_error \| internal_error), traceback? |
| `model_response` | step | **reserved** — raw provider response when a real adapter's output differs from the normalized action |

Two payload fields are load-bearing downstream: `side_effect` on
`tool_call_executed` (attribution finds the first irreversible action by
it) and `status` on retrieval results (verifier provenance and the
dashboard's status badges).

## Guarantees

- **Write-through:** events flush to disk as they happen, so a dying run
  keeps its partial trace. Caught failures record an `error` event and the
  runner still appends the final snapshot and `run_finished`; only a hard
  kill truncates the trace mid-stream.
- **Append-only:** traces are never edited after a run. Re-analysis writes
  new artifacts, never rewrites evidence.
- **Self-contained lines:** each line parses independently
  (`TraceRecorder.read_jsonl` round-trips, tested).

## Intended evolution (decided now, built later)

1. **Typed payloads:** payloads become per-event-type Pydantic models
   (discriminated unions on `event_type`) once the dashboard's first read
   pass shows what it consumes. Consumers should not string-index dicts
   forever.
2. **Nested spans:** `parent_event_id` for sub-agent calls and retries.
3. **Structured citations:** model actions that reference docs should carry
   doc_ids structurally (today: substring matching of reasoning text).

Any of these = schema_version bump + consumer coordination (tests,
dashboard, API) in the same PR.
