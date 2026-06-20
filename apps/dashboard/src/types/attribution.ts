/**
 * Attribution data contract.
 *
 * Mirrors `src/trace_harness/attribution/schemas.py` 
 * (TRA-13, PR #65, ATTRIBUTION_SCHEMA_VERSION 0.3.0). 
 * Category definitions: docs/failure_taxonomy.md.
 */

import { camelizeKeys, type Camelize } from "@/lib/casing";

export const ATTRIBUTION_SCHEMA_VERSION = "0.3.0";

/**
 * Failure taxonomy. Additive-only, extend, never repurpose or remove.
 */
export const FAILURE_CATEGORIES = [
  // Upstream
  /** Acted on a silent assumption instead of asking when state was ambiguous. */
  "clarification_failure",
  /** The plan itself was wrong, independent of ambiguity. */
  "planning_error",
  // Retrieval and reasoning
  /** The search was malformed; the right document never had a chance to surface. */
  "query_formation_error",
  /** Good results came back; agent picked the wrong one. */
  "retrieval_selection_error",
  /** Picked a deprecated source and treated it as current. */
  "stale_source_authority",
  /** Inputs were correct; agent still drew the wrong conclusion. */
  "reasoning_commitment_error",
  /** Lost track of something that happened in this run. */
  "state_tracking_error",
  /** Imported a fact never true for this run (stale/other-conversation). */
  "context_memory_leakage",
  /** Marker: had disconfirming evidence and proceeded anyway. */
  "missed_recovery",
  // Action
  /** Wrong (or nonexistent) tool for the situation — mechanically clean. */
  "tool_selection_error",
  /** Right tool, executed as intended, action forbidden by policy. */
  "unsafe_irreversible_action",
  /** Right tool and intent; the call itself misfired (bad arg, exception). */
  "tool_implementation_error",
  /** Rule break that isn't selection, irreversibility, or implementation. */
  "policy_violation",
  // Output
  /** Claim implies source support it doesn't have; spoken, not persisted. */
  "grounding_citation_error",
  /** Same as grounding error, but persisted (ticket, record). */
  "false_durable_record",
  /** The agent's own summary contradicts what it actually did. */
  "inconsistent_final_answer",
  // Trajectory-level
  /** Stopped before completing — gave up / out of budget, no honest attempt. */
  "premature_termination",
  /** Repeats an equivalent action/query without progress. */
  "unproductive_loop",
  // Fallback
  /** Trace gives no usable evidence; pair with an ambiguity note. */
  "unknown",
] as const;

export type FailureCategory = (typeof FAILURE_CATEGORIES)[number];

/**
 * Wire shape of `attribution_result.json`, defined in the backend
 * Pydantic model (snake_case keys).
 */
export interface RawAttributionResult {
  schema_version: string;
  run_id: string;
  /** Where the failure causally began (refund fixture: step 3). */
  root_cause_step: number | null;
  /** Earliest detectably-wrong step; may precede the root cause. */
  first_bad_step: number | null;
  /** A step where the agent had evidence to recover and didn't. */
  missed_recovery_step: number | null;
  /** Earliest step after which no recovery path existed. */
  first_unrecoverable_step: number | null;
  /** First external action that cannot be taken back (refund fixture: step 5). */
  first_irreversible_action_step: number | null;
  /** Steps where the failure is externally observable. */
  visible_symptom_steps: number[];
  primary_failure_category: FailureCategory;
  contributing_failure_categories: FailureCategory[];
  causal_explanation: string;
  /** Union of the step fields above — drives FailureCard.evidence. */
  evidence_step_ids: number[];
  /** 0..1; heuristic attributions stay well below 1.0 by policy. */
  confidence: number;
  ambiguity_notes: string[];
  metadata: Record<string, unknown>;
}

/** "Where did this go wrong, and why", the camelCase domain type. */
export type AttributionResult = Camelize<RawAttributionResult>;

export const parseAttributionResult = (
  raw: RawAttributionResult,
): AttributionResult => camelizeKeys(raw);
