/**
 * Trace-event data contract.
 *
 * Mirrors `TraceEvent` in `src/trace_harness/tracing/events.py`
 * (TRACE_SCHEMA_VERSION 0.2.0), serialized as `runs/{run_id}/trace.jsonl`
 * (one JSON object per line).
 *
 * The structured log of what happened during a run. Every `stepId` referenced
 * by evidence, failed checks, and attribution points into this trace. Unlike
 * the single-object artifacts, a trace is a list of events, so this contract
 * exposes both a single-event parser and a list helper.
 */

import { camelizeKeys, type Camelize } from "@/lib/casing";

export const TRACE_SCHEMA_VERSION = "0.2.0";

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

export interface RawRunStartedPayload {
  task_id: string;
  provider: string;
  model: string | null;
  max_steps: number;
  timeout_seconds: number;
  prompt_version: string;
}

export interface RawTaskLoadedPayload {
  task: Record<string, unknown>;
}

export interface RawStateSnapshotPayload {
  phase: string;
  state: Record<string, unknown>;
}

export interface RawModelPromptPayload {
  transcript_length: number;
  new_messages: Record<string, unknown>[];
}

export interface RawModelResponsePayload {
  raw?: Record<string, unknown> | null;
}

export interface RawModelActionPayload {
  kind: string;
  tool_call?: Record<string, unknown> | null;
  final_answer?: string | null;
  reasoning?: string | null;
}

export interface RawToolCallRequestedPayload {
  tool_name: string;
  arguments: Record<string, unknown>;
}

export interface RawToolCallValidatedPayload {
  tool_name: string;
  valid: boolean;
  error?: string | null;
}

export interface RawToolCallExecutedPayload {
  tool_name: string;
  arguments: Record<string, unknown>;
  status: string;
  side_effect?: string | null;
  error?: string | null;
}

export interface RawToolObservationPayload {
  tool_name: string;
  status: string;
  result?: unknown;
  error?: string | null;
}

export interface RawRetrievalResultPayload {
  query?: string | null;
  result_count: number;
  results: Record<string, unknown>[];
}

export interface RawFinalAnswerPayload {
  final_answer: string;
}

export interface RawRunFinishedPayload {
  status: string;
  termination_reason: string;
  steps_taken: number;
}

export interface RawErrorPayload {
  error: string;
  kind: string;
  traceback?: string | null;
}

/** Keeps event_type and payload correlated as a discriminated union. */
export interface TracePayloadByType {
  run_started: RawRunStartedPayload;
  task_loaded: RawTaskLoadedPayload;
  state_snapshot: RawStateSnapshotPayload;
  model_prompt: RawModelPromptPayload;
  model_response: RawModelResponsePayload;
  model_action: RawModelActionPayload;
  tool_call_requested: RawToolCallRequestedPayload;
  tool_call_validated: RawToolCallValidatedPayload;
  tool_call_executed: RawToolCallExecutedPayload;
  tool_observation: RawToolObservationPayload;
  retrieval_result: RawRetrievalResultPayload;
  final_answer: RawFinalAnswerPayload;
  run_finished: RawRunFinishedPayload;
  error: RawErrorPayload;
}

/**
 * Fields common to every line in `trace.jsonl` (snake_case wire keys).
 */
interface RawTraceEventEnvelope {
  schema_version: string;
  event_id: string;
  run_id: string;
  /**
   * Agent decision step, counting from 1; all events caused by one decision
   * share it. `null` for run-level events (run_started, snapshots, run_finished).
   */
  step_id: number | null;
  /** ISO 8601 timestamp; backend serializes `datetime` as a string. */
  timestamp: string;
  metadata: Record<string, unknown>;
  /** Parent event in the same run; null for top-level events. */
  parent_event_id: string | null;
}

/** Wire event union discriminated by event_type. */
export type RawTraceEvent<T extends TraceEventType = TraceEventType> = {
  [K in T]: RawTraceEventEnvelope & {
    event_type: K;
    payload: TracePayloadByType[K];
  };
}[T];

/** One structured event in a run's trace, the camelCase domain type. */
export type TraceEvent<T extends TraceEventType = TraceEventType> = Camelize<
  RawTraceEvent<T>
>;

export const parseTraceEvent = <T extends TraceEventType>(
  raw: RawTraceEvent<T>,
): TraceEvent<T> => camelizeKeys(raw);

/** Parse a full trace (the parsed lines of `trace.jsonl`) into domain events. */
export const parseTrace = (raw: RawTraceEvent[]): TraceEvent[] =>
  raw.map(parseTraceEvent);
