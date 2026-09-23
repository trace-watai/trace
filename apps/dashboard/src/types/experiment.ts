/**
 * Experiment data contract.
 *
 * Mirrors `ExperimentSpec` / `ExperimentResult` in
 * `src/trace_harness/runner/experiment.py` (EXPERIMENT_SCHEMA_VERSION 0.3.0;
 * 0.2.0 added the frozen set in #195 and 0.3.0 `continuation_script` in #159),
 * serialized as `experiment.json` and `result.json` under
 * `runs/experiments/{experiment_id}/`.
 *
 * An experiment relates several batches to one question. The plan is written
 * before anything runs; the result records which batch answered which
 * condition and what was decided on the evidence.
 */

import { camelizeKeys, type Camelize } from "@/lib/casing";

export const EXPERIMENT_SCHEMA_VERSION = "0.3.0";

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
  /** The outside agent, as `package.module:factory`, when provider is `external` (#210). */
  agent_ref?: string | null;
}

export interface RawConditionSpec {
  name: string;
  kind: ConditionKind;
  agent_config: RawAgentConfig;
  control_ids: string[];
  seeds: number[];
  start?: RawStartPoint | null;
  /** Fixture script played after the start step; absent means the recording (0.3.0). */
  continuation_script?: string | null;
}

/**
 * One frozen path from `runner/frozen_set.py`: a sha256 per file, keyed by
 * repo-relative path, and one digest over all of them.
 */
export interface RawFrozenComponent {
  path: string;
  digest: string;
  files: Record<string, string>;
}

/** One file that differed from the plan's frozen set at record time. */
export interface RawFrozenFileChange {
  component: string;
  path: string;
  change: "changed" | "added" | "removed";
}

/**
 * What must not change while the conditions run. If `fixtures_hash` differs
 * between two conditions they answered different questions, and comparing
 * them is void. `frozen_set` covers the verifier, environment, attribution
 * scorer, suite, fixtures and labels, keyed by component name; it is absent
 * on plans from schema 0.1.0.
 */
export interface RawFrozenManifest {
  suite_id: string;
  verifier_ids: string[];
  fixtures_hash: string;
  labels_path?: string | null;
  frozen_set?: Record<string, RawFrozenComponent> | null;
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
  /**
   * Absent on results from schema 0.1.0 and read as false. Both false means
   * the plan had no frozen set, so nothing was checked; a drifted result
   * always has decision `review`.
   */
  frozen_set_verified?: boolean;
  frozen_set_drifted?: boolean;
  frozen_set_drift?: RawFrozenFileChange[];
}

/**
 * `frozenSet` is keyed by component name and each component's `files` by
 * repo-relative path. Camelizing would turn `src/trace_harness/...` into
 * `src/traceHarness/...`, so the map is restored from the raw payload.
 */
export type FrozenManifest = Omit<Camelize<RawFrozenManifest>, "frozenSet"> & {
  frozenSet: Record<string, RawFrozenComponent> | null;
};

export type ExperimentSpec = Omit<
  Camelize<RawExperimentSpec>,
  "frozenManifest"
> & {
  frozenManifest: FrozenManifest;
};

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
  | "conditionBatches"
  | "metrics"
  | "frozenSetVerified"
  | "frozenSetDrifted"
  | "frozenSetDrift"
> & {
  conditionBatches: Record<string, string>;
  metrics: ExperimentMetrics;
  frozenSetVerified: boolean;
  frozenSetDrifted: boolean;
  frozenSetDrift: RawFrozenFileChange[];
};

export const parseExperimentSpec = (raw: RawExperimentSpec): ExperimentSpec => {
  const spec = camelizeKeys(raw);
  const frozenSet = raw.frozen_manifest.frozen_set;
  return {
    ...spec,
    frozenManifest: {
      ...spec.frozenManifest,
      frozenSet: frozenSet
        ? Object.fromEntries(
            Object.entries(frozenSet).map(([name, component]) => [
              name,
              { ...component, files: { ...component.files } },
            ]),
          )
        : null,
    },
  };
};

export const parseExperimentResult = (
  raw: RawExperimentResult,
): ExperimentResult => ({
  ...(camelizeKeys(raw) as Camelize<RawExperimentResult>),
  frozenSetVerified: raw.frozen_set_verified ?? false,
  frozenSetDrifted: raw.frozen_set_drifted ?? false,
  frozenSetDrift: (raw.frozen_set_drift ?? []).map((change) => ({
    ...change,
  })),
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
