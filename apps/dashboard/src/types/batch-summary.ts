/**
 * Batch summary data contract.
 *
 * Mirrors `BatchSummary` and `BatchRunEntry` in
 * `src/trace_harness/runner/batch.py` (BATCH_SUMMARY_SCHEMA_VERSION 0.4.0),
 * serialized as `runs/batches/{batch_id}/batch_summary.json`. Schema 0.2.0
 * added the per-entry verdict and `aggregates.incomplete`, 0.3.0 the optional
 * `budget` block (#196), and 0.4.0 the branch stage's per-entry fields and
 * summary metadata (#159). Files from 0.1.0 on lack some of these, so every
 * field added after 0.1.0 is optional here.
 */

import { camelizeKeys, type Camelize } from "@/lib/casing";
import type { PostBlockOutcome } from "@/types/attribution";
import type { RawAgentConfig } from "@/types/experiment";

export const BATCH_SUMMARY_SCHEMA_VERSION = "0.4.0";

/** One run in the batch: one task under one agent config, or one branch seed. */
export interface RawBatchRunEntry {
  /** Null when setup failed before a run existed. */
  run_id: string | null;
  task_id: string;
  task_path: string;
  task_schema_version?: string | null;
  agent_label: string;
  provider: string;
  model?: string | null;
  prompt_version?: string | null;
  /** completed / terminated / error / setup_error */
  status: string;
  termination_reason?: string | null;
  steps_taken?: number | null;
  verifier_passed?: boolean | null;
  /** pass / fail / incomplete; null when verification did not run; absent before 0.2.0. */
  verdict?: string | null;
  verifier_id?: string | null;
  severity?: string | null;
  latency_ms?: number | null;
  /** Null is unknown cost, which is different from zero. */
  cost_usd?: number | null;
  error?: string | null;
  /** Branch stage fields (0.4.0); null on suite entries. */
  condition?: string | null;
  seed?: number | null;
  /** Step where the run first differed from the recording after the fork. */
  first_post_fork_divergence_step?: number | null;
  /** Whether the first action after the fork differed from the recording. */
  diverged?: boolean | null;
  post_block_outcome?: PostBlockOutcome | null;
}

export interface RawBatchAggregates {
  total: number;
  completed: number;
  terminated: number;
  errored: number;
  /** Absent before 0.2.0. */
  incomplete?: number;
  verifier_passed: number;
  verifier_failed: number;
  cost_recorded: number;
  known_cost_usd: number;
  pass_rate?: number | null;
  /** agent label -> counts */
  by_agent: Record<string, Record<string, number>>;
}

export const BUDGET_STOP_REASONS = [
  "budget_exhausted",
  "budget_unenforceable",
] as const;

export type BudgetStopReason = (typeof BUDGET_STOP_REASONS)[number];

/** A cell the budget guard never started. */
export interface RawNotRunCell {
  agent_label: string;
  task_path: string;
}

/** What a capped batch spent, and why it stopped early if it did (0.3.0). */
export interface RawBatchBudget {
  max_cost_usd: number;
  /** Recorded cost of the batch's live runs; fixture and replay runs add nothing. */
  spent_usd: number;
  stop_reason?: BudgetStopReason | null;
  detail?: string | null;
  not_run: RawNotRunCell[];
}

export interface RawBatchSummary {
  schema_version: string;
  batch_id: string;
  suite_id: string;
  started_at: string;
  finished_at: string;
  agent_configs: RawAgentConfig[];
  entries: RawBatchRunEntry[];
  aggregates: RawBatchAggregates;
  /** Present when the batch ran under a spend cap; absent before 0.3.0. */
  budget?: RawBatchBudget | null;
  /** Branch batches: experiment_id, condition, condition_kind, source_run_id, start. */
  metadata?: Record<string, unknown>;
}

type Aggregates = Omit<Camelize<RawBatchAggregates>, "byAgent"> & {
  byAgent: Record<string, Record<string, number>>;
};

export type BatchSummary = Omit<Camelize<RawBatchSummary>, "aggregates"> & {
  aggregates: Aggregates;
};

/**
 * `byAgent` is keyed by agent label, which is data, so it is restored from the
 * raw payload after camelizing, the way the experiment mirror keeps condition
 * names intact.
 */
export const parseBatchSummary = (raw: RawBatchSummary): BatchSummary => {
  const camel = camelizeKeys(raw) as Camelize<RawBatchSummary>;
  return {
    ...camel,
    aggregates: {
      ...camel.aggregates,
      byAgent: { ...raw.aggregates.by_agent },
    },
  };
};
