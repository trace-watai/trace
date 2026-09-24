/**
 * Sweep summary data contract.
 *
 * Mirrors `SweepSummary` in `src/trace_harness/runner/sweep_summary.py`
 * (SWEEP_SUMMARY_SCHEMA_VERSION 0.1.0), serialized by `trace-harness
 * run-sweep` as `runs/sweeps/{sweep_id}/sweep_summary.json` and retained
 * beside the failing cells under `docs/acceptance/runs/live-sweep-<date>/`
 * (#198). `docs/live_sweep.md` defines every count and the two labels.
 */

import { camelizeKeys, type Camelize } from "@/lib/casing";
import type { RawBatchBudget } from "@/types/batch-summary";

export const SWEEP_SUMMARY_SCHEMA_VERSION = "0.1.0";

export const FAILURE_LABELS = ["staged_trap", "natural"] as const;

export type FailureLabel = (typeof FAILURE_LABELS)[number];

/** One task under one provider, counted over the sweep's seeds. */
export interface RawSweepTaskRow {
  provider_label: string;
  task_id: string;
  task_path: string;
  passed: number;
  failed: number;
  incomplete: number;
  not_run: number;
  /** At least one seed passed and at least one failed. */
  flipped: boolean;
}

/** One run that completed with verdict `fail`, and its label. */
export interface RawSweepFailingCell {
  provider_label: string;
  provider: string;
  model: string;
  seed: number;
  task_id: string;
  task_path: string;
  run_id: string;
  batch_id: string;
  failed_check_ids: string[];
  /** A release-blocking check fired, so this is a verified failure. */
  blocking: boolean;
  label: FailureLabel;
  /** The checks that made the cell natural; empty on a staged trap. */
  natural_check_ids: string[];
  label_reason: string;
  /** Null is unknown cost, which is different from zero. */
  cost_usd: number | null;
  /** Relative to the sweep directory. */
  cassette_path: string;
}

/** One provider's batch, counted over every task and seed. */
export interface RawSweepProviderResult {
  label: string;
  provider: string;
  model: string;
  batch_id: string;
  runs: number;
  passed: number;
  failed: number;
  incomplete: number;
  not_run: number;
  flipped_tasks: number;
  verified_failures: number;
  cost_usd: number;
  cost_recorded: number;
}

export interface RawSweepSummary {
  schema_version: string;
  sweep_id: string;
  sweep_name: string;
  spec_path: string | null;
  suite_id: string;
  started_at: string;
  finished_at: string;
  seeds: number[];
  task_count: number;
  providers: RawSweepProviderResult[];
  tasks: RawSweepTaskRow[];
  /** Distinct tasks that flipped under at least one provider. */
  flipped_tasks: number;
  runs: number;
  cost_usd: number;
  cost_recorded: number;
  verified_failures: number;
  natural_verified_failures: number;
  /** Null when nothing failed or any run is missing its cost. */
  cost_per_verified_failure: number | null;
  cost_per_natural_verified_failure: number | null;
  failing_cells: RawSweepFailingCell[];
  budget: RawBatchBudget | null;
}

export type SweepSummary = Camelize<RawSweepSummary>;

export const parseSweepSummary = (raw: RawSweepSummary): SweepSummary =>
  camelizeKeys(raw);
