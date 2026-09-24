/**
 * Metrics-history data contract.
 *
 * Mirrors `MetricsSnapshot` in `src/trace_harness/metrics/history.py`
 * (METRICS_SNAPSHOT_SCHEMA_VERSION 0.3.0), serialized one record per line into
 * `docs/acceptance/metrics_history.jsonl` by the main workflow after a merge.
 * Lines written at an older version stay in that file and are read here too.
 *
 * Coverage, over-blocking and cost of learning are trends. Each carries the two
 * counts it came from so a point can be read without trusting a rate, and a
 * rate over an empty denominator is null rather than zero, since nothing
 * measured and nothing covered are different facts.
 */

import { camelizeKeys, type Camelize } from "@/lib/casing";
import { clopperPearsonUpper, roundUp } from "@/lib/over-blocking";

export const METRICS_SNAPSHOT_SCHEMA_VERSION = "0.3.0";

/** Versions that can appear in the committed history, oldest first. */
export const READABLE_METRICS_SNAPSHOT_VERSIONS = [
  "0.1.0",
  "0.2.0",
  "0.3.0",
] as const;

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
  /**
   * `accepted` split by standing: gating when a verdict was reached against a
   * `static_ok` artifact, advisory otherwise. Absent before 0.2.0.
   */
  accepted_gating?: number;
  accepted_advisory?: number;
  accepted_over_prescribed: RawRatio;
  materializable_over_prescribed: RawRatio;
  unmapped_controls: string[];
}

export interface RawOverBlocking {
  siblings_run: number;
  siblings_failed: number;
  rate: RawRatio;
  /**
   * Distinct task families among completed siblings, and how many had a
   * failing sibling. Absent before 0.3.0.
   */
  independent_families?: number | null;
  families_failed?: number | null;
  /**
   * One-sided 95% Clopper-Pearson bound on the family failure rate, rounded
   * up to four places. Derived by the backend and recomputed here from the
   * two family counts the same way.
   */
  upper_bound_95?: number | null;
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
export type Coverage = Camelize<Required<RawCoverage>>;
export type OverBlocking = Camelize<Required<RawOverBlocking>>;
export type CostOfLearning = Camelize<RawCostOfLearning>;
export type MetricsSnapshot = Omit<
  Camelize<RawMetricsSnapshot>,
  "coverage" | "overBlocking"
> & {
  coverage: Coverage;
  overBlocking: OverBlocking;
};

/**
 * The rate a ratio describes, or null when nothing was measured.
 *
 * Recomputed from the two counts rather than read off `value`, so a
 * hand-edited history line cannot make a chart disagree with its own numbers.
 * The backend drops a supplied `value` for the same reason.
 */
export const ratioValue = (ratio: Ratio): number | null =>
  ratio.denominator === 0 ? null : ratio.numerator / ratio.denominator;

/**
 * Fill the gating and advisory split on a record written before it existed.
 *
 * Those records were computed from validations that carried no replay mode,
 * and a verdict without one reads as not recorded, which is advisory. The
 * backend reads the same lines the same way. A split that does not add up to
 * `accepted` throws, as the backend rejects it.
 */
const withAcceptanceSplit = (coverage: Camelize<RawCoverage>): Coverage => {
  const acceptedGating = coverage.acceptedGating ?? 0;
  const acceptedAdvisory = coverage.acceptedAdvisory ?? coverage.accepted;
  if (acceptedGating + acceptedAdvisory !== coverage.accepted) {
    throw new RangeError(
      `gating and advisory acceptances must sum to accepted, got ${acceptedGating} + ${acceptedAdvisory} for ${coverage.accepted}`,
    );
  }
  return { ...coverage, acceptedGating, acceptedAdvisory };
};

/**
 * Family counts as recorded, with the bound derived from them.
 *
 * A record from before 0.3.0 has no family counts. They cannot be recovered
 * from sibling totals, so they stay null and the page shows no bound.
 */
const withFamilyBound = (
  overBlocking: Camelize<RawOverBlocking>,
): OverBlocking => {
  const independentFamilies = overBlocking.independentFamilies ?? null;
  const familiesFailed = overBlocking.familiesFailed ?? null;
  const bound =
    independentFamilies === null || familiesFailed === null
      ? null
      : clopperPearsonUpper(familiesFailed, independentFamilies);
  return {
    ...overBlocking,
    independentFamilies,
    familiesFailed,
    upperBound95: bound === null ? null : roundUp(bound),
  };
};

export const parseMetricsSnapshot = (
  raw: RawMetricsSnapshot,
): MetricsSnapshot => {
  const snapshot = camelizeKeys(raw);
  return {
    ...snapshot,
    coverage: withAcceptanceSplit(snapshot.coverage),
    overBlocking: withFamilyBound(snapshot.overBlocking),
  };
};

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
