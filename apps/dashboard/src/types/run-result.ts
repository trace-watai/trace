/**
 * Run-result data contract.
 *
 * Mirrors `RunResult` in `src/trace_harness/runner/result.py`
 * (RUN_RESULT_SCHEMA_VERSION 0.1.0), serialized as `run_result.json`.
 *
 * How a run *ended*, distinct from whether it was *correct* (that is the
 * verifier's job). A run can be `completed` and still fail verification.
 * This is where the dashboard reads the run's timestamps, status chips, and
 * artifact paths that the failure card itself does not carry.
 */

import { camelizeKeys, type Camelize } from "@/lib/casing";

export const RUN_RESULT_SCHEMA_VERSION = "0.1.0";

/**
 * How the run terminated at the harness level.
 * - `completed`: reached a final answer
 * - `terminated`: stopped by harness limits (steps/timeout/script end)
 * - `error`: adapter or harness exception
 */
export const RUN_STATUSES = ["completed", "terminated", "error"] as const;

export type RunStatus = (typeof RUN_STATUSES)[number];

/** Why the run stopped; pairs with (but is finer-grained than) RunStatus. */
export const TERMINATION_REASONS = [
  "final_answer",
  "max_steps_reached",
  "timeout",
  "script_exhausted",
  "model_error",
  "internal_error",
] as const;

export type TerminationReason = (typeof TERMINATION_REASONS)[number];

/**
 * Wire shape of `run_result.json`, defined in the backend Pydantic model
 * (snake_case keys).
 */
export interface RawRunResult {
  schema_version: string;
  run_id: string;
  task_id: string;
  status: RunStatus;
  termination_reason: TerminationReason;
  steps_taken: number;
  /** The agent's final user-facing output, when it produced one. */
  final_output: string | null;
  /** Artifact name -> path relative to the runs directory, for the dashboard. */
  artifact_paths: Record<string, string>;
  /** ISO 8601 timestamp; backend serializes `datetime` as a string. */
  started_at: string;
  /** ISO 8601 timestamp; backend serializes `datetime` as a string. */
  finished_at: string;
  /** Set only when `status === "error"`. */
  error: string | null;
}

/** "How the run ended", the camelCase domain type. */
export type RunResult = Camelize<RawRunResult>;

export const parseRunResult = (raw: RawRunResult): RunResult =>
  camelizeKeys(raw);
