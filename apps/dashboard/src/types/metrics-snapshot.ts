/**
 * Metrics-history data contract.
 *
 * Mirrors `MetricsSnapshot` in `src/trace_harness/metrics/history.py`
 * (METRICS_SNAPSHOT_SCHEMA_VERSION 0.1.0), serialized one record per line into
 * `docs/acceptance/metrics_history.jsonl` by the main workflow after a merge.
 *
 * Coverage, over-blocking and cost of learning are trends. Each carries the two
 * counts it came from so a point can be read without trusting a rate, and a
 * rate over an empty denominator is null rather than zero, since nothing
 * measured and nothing covered are different facts.
 */

import { camelizeKeys, type Camelize } from "@/lib/casing";

export const METRICS_SNAPSHOT_SCHEMA_VERSION = "0.1.0";

export interface RawRatio {
  numerator: number;
  denominator: number;
  /** Derived by the backend. Recomputed here rather than trusted. */
  value?: number | null;
}

export interface RawCoverage {
  prescribed: number;
  materializable: number;
  validated: number;
  accepted: number;
  accepted_over_prescribed: RawRatio;
  materializable_over_prescribed: RawRatio;
  unmapped_controls: string[];
}

export interface RawOverBlocking {
  siblings_run: number;
  siblings_failed: number;
  rate: RawRatio;
  sources: string[];
}

export interface RawCostOfLearning {
  validation_runs: number;
  irreversible_actions: number;
  money_moved_usd: number;
  runs_not_retained: string[];
}

export interface RawMetricsSnapshot {
  schema_version: string;
  commit: string;
  recorded_at: string;
  coverage: RawCoverage;
  over_blocking: RawOverBlocking;
  cost_of_learning: RawCostOfLearning;
  suite_pass_rate: RawRatio;
  verified_failures: number;
}

export type Ratio = Camelize<RawRatio>;
export type Coverage = Camelize<RawCoverage>;
export type OverBlocking = Camelize<RawOverBlocking>;
export type CostOfLearning = Camelize<RawCostOfLearning>;
export type MetricsSnapshot = Camelize<RawMetricsSnapshot>;

/**
 * The rate a ratio describes, or null when nothing was measured.
 *
 * Recomputed from the two counts rather than read off `value`, so a
 * hand-edited history line cannot make a chart disagree with its own numbers.
 * The backend drops a supplied `value` for the same reason.
 */
export const ratioValue = (ratio: Ratio): number | null =>
  ratio.denominator === 0 ? null : ratio.numerator / ratio.denominator;

export const parseMetricsSnapshot = (
  raw: RawMetricsSnapshot,
): MetricsSnapshot => camelizeKeys(raw);

/**
 * Parse a JSONL history file, oldest first, skipping blank lines.
 *
 * A malformed line throws. A history the page cannot read is worth knowing
 * about, unlike the writer's case where a bad file must not cost a record.
 */
export const parseMetricsHistory = (contents: string): MetricsSnapshot[] =>
  contents
    .split("\n")
    .filter((line) => line.trim().length > 0)
    .map((line) =>
      parseMetricsSnapshot(JSON.parse(line) as RawMetricsSnapshot),
    );
