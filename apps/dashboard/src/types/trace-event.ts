/**
 * Trace-event data contract.
 *
 * Mirrors `TraceEvent` in `src/trace_harness/tracing/events.py`
 * (TRACE_SCHEMA_VERSION 0.1.0), serialized as `runs/{run_id}/trace.jsonl`
 * (one JSON object per line).
 *
 * The structured log of what happened during a run. Every `stepId` referenced
 * by evidence, failed checks, and attribution points into this trace. Unlike
 * the single-object artifacts, a trace is a list of events, so this contract
 * exposes both a single-event parser and a list helper.
 */

import { camelizeKeys, type Camelize } from "@/lib/casing";

export const TRACE_SCHEMA_VERSION = "0.1.0";

/**
 * Every kind of event a run may emit (mirrors the backend `TraceEventType`
 * StrEnum). MVP runs emit a subset: the fixture adapter produces no separate
 * `model_response`, which is reserved for real provider adapters whose raw
 * response differs from the normalized action.
 */
export const TRACE_EVENT_TYPES = [
  "run_started",
  "task_loaded",
  "state_snapshot",
  "model_prompt",
  "model_response",
  "model_action",
  "tool_call_requested",
  "tool_call_validated",
  "tool_call_executed",
  "tool_observation",
  "retrieval_result",
  "final_answer",
  "run_finished",
  "error",
] as const;

export type TraceEventType = (typeof TRACE_EVENT_TYPES)[number];

/**
 * Wire shape of one line in `trace.jsonl`, defined in the backend Pydantic
 * model (snake_case keys).
 */
export interface RawTraceEvent {
  schema_version: string;
  event_id: string;
  run_id: string;
  /**
   * Agent decision step, counting from 1; all events caused by one decision
   * share it. `null` for run-level events (run_started, snapshots, run_finished).
   */
  step_id: number | null;
  event_type: TraceEventType;
  /** ISO 8601 timestamp; backend serializes `datetime` as a string. */
  timestamp: string;
  /** Event-specific facts. Untyped for now; the backend defers typed payloads. */
  payload: Record<string, unknown>;
  metadata: Record<string, unknown>;
}

/** One structured event in a run's trace, the camelCase domain type. */
export type TraceEvent = Camelize<RawTraceEvent>;

export const parseTraceEvent = (raw: RawTraceEvent): TraceEvent =>
  camelizeKeys(raw);

/** Parse a full trace (the parsed lines of `trace.jsonl`) into domain events. */
export const parseTrace = (raw: RawTraceEvent[]): TraceEvent[] =>
  raw.map(parseTraceEvent);
