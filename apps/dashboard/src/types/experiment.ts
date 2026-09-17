/**
 * Experiment data contract.
 *
 * Mirrors `ExperimentSpec` / `ExperimentResult` in
 * `src/trace_harness/runner/experiment.py` (EXPERIMENT_SCHEMA_VERSION 0.1.0),
 * serialized as `experiment.json` and `result.json` under
 * `runs/experiments/{experiment_id}/`.
 *
 * An experiment relates several batches to one question. The plan is written
 * before anything runs; the result records which batch answered which
 * condition and what was decided on the evidence.
 */

import { camelizeKeys, type Camelize } from "@/lib/casing";

export const EXPERIMENT_SCHEMA_VERSION = "0.1.0";

/**
 * What a condition does to produce its runs. `static_replay` re-runs recorded
 * actions and cannot react to being blocked, which is the limitation the
 * replay-validity question exists to measure.
 */
export const CONDITION_KINDS = [
  "static_replay",
  "live",
  "live_no_control",
  "live_swapped",
] as const;

export type ConditionKind = (typeof CONDITION_KINDS)[number];

export const DECISIONS = ["baseline", "keep", "discard", "review"] as const;

export type Decision = (typeof DECISIONS)[number];

export const DECIDED_BY = ["human", "policy"] as const;

export type DecidedBy = (typeof DECIDED_BY)[number];

/** Where in a recorded run a condition begins, when not from the beginning. */
export interface RawStartPoint {
  source_run_id: string;
  step_id: number;
}

export interface RawAgentConfig {
  label: string;
  provider: string;
  model?: string | null;
  prompt_version?: string | null;
  temperature?: number | null;
  seed?: number | null;
  max_steps: number;
  timeout_seconds: number;
}

export interface RawConditionSpec {
  name: string;
  kind: ConditionKind;
  agent_config: RawAgentConfig;
  control_ids: string[];
  seeds: number[];
  start?: RawStartPoint | null;
}

/**
 * What must not change while the conditions run. If `fixtures_hash` differs
 * between two conditions they answered different questions, and comparing
 * them is void.
 */
export interface RawFrozenManifest {
  suite_id: string;
  verifier_ids: string[];
  fixtures_hash: string;
}

export interface RawBudget {
  max_runs: number;
  max_cost_usd: number;
}

export interface RawExperimentSpec {
  schema_version: string;
  experiment_id: string;
  brief_path?: string | null;
  hypothesis: string;
  frozen_manifest: RawFrozenManifest;
  conditions: RawConditionSpec[];
  budget: RawBudget;
  created_at: string;
  metadata: Record<string, unknown>;
}

/**
 * The eight metrics named in the #27 memo, and nothing else. Every one is
 * nullable: a condition set that never ran live cannot produce the divergence
 * rates, and a missing number is not zero. There is deliberately no combined
 * score.
 */
export interface RawExperimentMetrics {
  verdict_agreement_rate?: number | null;
  first_post_fork_divergence_rate?: number | null;
  noise_floor_divergence_rate?: number | null;
  post_block_outcomes?: Record<string, number> | null;
  sibling_failure_rate?: number | null;
  verified_failure_count?: number | null;
  cost_usd?: number | null;
  latency_ms_p50?: number | null;
  extra: Record<string, number>;
}

export interface RawExperimentResult {
  schema_version: string;
  experiment_id: string;
  /** condition name -> the batch id that answered it */
  condition_batches: Record<string, string>;
  metrics: RawExperimentMetrics;
  decision: Decision;
  decided_by: DecidedBy;
  report_path?: string | null;
  finished_at: string;
  metadata: Record<string, unknown>;
}

export type ExperimentSpec = Camelize<RawExperimentSpec>;

/**
 * `camelizeKeys` rewrites every object key it meets, which is right for field
 * names and wrong for maps whose keys are data. `conditionBatches` is keyed by
 * condition name, `postBlockOutcomes` by outcome label and `extra` by metric
 * name, and all three have to round-trip byte for byte to match the plan and
 * the memo. So those are restored from the raw payload after camelizing.
 */
export type ExperimentMetrics = Omit<
  Camelize<RawExperimentMetrics>,
  "postBlockOutcomes" | "extra"
> & {
  postBlockOutcomes?: Record<string, number> | null;
  extra: Record<string, number>;
};

export type ExperimentResult = Omit<
  Camelize<RawExperimentResult>,
  "conditionBatches" | "metrics"
> & {
  conditionBatches: Record<string, string>;
  metrics: ExperimentMetrics;
};

export const parseExperimentSpec = (raw: RawExperimentSpec): ExperimentSpec =>
  camelizeKeys(raw);

export const parseExperimentResult = (
  raw: RawExperimentResult,
): ExperimentResult => ({
  ...(camelizeKeys(raw) as Camelize<RawExperimentResult>),
  conditionBatches: { ...raw.condition_batches },
  metrics: {
    ...(camelizeKeys(raw.metrics) as Camelize<RawExperimentMetrics>),
    postBlockOutcomes: raw.metrics.post_block_outcomes
      ? { ...raw.metrics.post_block_outcomes }
      : raw.metrics.post_block_outcomes,
    extra: { ...raw.metrics.extra },
  },
});

/**
 * Metric field names in the order the memo lists them, for table headers.
 * Kept in sync with the backend by `tests/test_experiment.py`, which asserts
 * the Python field set against the memo's appendix.
 */
export const EXPERIMENT_METRIC_NAMES = [
  "verdictAgreementRate",
  "firstPostForkDivergenceRate",
  "noiseFloorDivergenceRate",
  "postBlockOutcomes",
  "siblingFailureRate",
  "verifiedFailureCount",
  "costUsd",
  "latencyMsP50",
] as const;
