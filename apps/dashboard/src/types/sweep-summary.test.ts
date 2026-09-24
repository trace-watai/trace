import { readFileSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

import {
  FAILURE_LABELS,
  SWEEP_SUMMARY_SCHEMA_VERSION,
  parseSweepSummary,
  type RawSweepFailingCell,
  type RawSweepProviderResult,
  type RawSweepSummary,
  type RawSweepTaskRow,
} from "@/types/sweep-summary";

const BACKEND = readFileSync(
  path.join(
    process.cwd(),
    "..",
    "..",
    "src",
    "trace_harness",
    "runner",
    "sweep_summary.py",
  ),
  "utf8",
);

/** The field names a pydantic class declares, in order. */
const pythonFields = (className: string): string[] => {
  const body = BACKEND.split(`\nclass ${className}(BaseModel):\n`)[1];
  const block = body.split(/\n(?=\S)/)[0];
  return [...block.matchAll(/^ {4}([a-z_][a-z0-9_]*): /gm)].map((m) => m[1]);
};

const row: RawSweepTaskRow = {
  provider_label: "gemini-3.6-flash",
  task_id: "refund_policy_valid_cash",
  task_path: "fixtures/tasks/refund_policy_valid_cash.json",
  passed: 4,
  failed: 1,
  incomplete: 0,
  not_run: 0,
  flipped: true,
};

const cell: RawSweepFailingCell = {
  provider_label: "gemini-3.6-flash",
  provider: "gemini",
  model: "gemini-3.6-flash",
  seed: 3,
  task_id: "refund_policy_valid_cash",
  task_path: "fixtures/tasks/refund_policy_valid_cash.json",
  run_id: "run_20260924T010317Z_a1eef11e",
  batch_id: "batch_20260924T010317Z_0ec0fb15",
  failed_check_ids: ["expected_refund_missing"],
  blocking: true,
  label: "natural",
  natural_check_ids: ["expected_refund_missing"],
  label_reason: "The task is a positive sibling that must keep passing.",
  cost_usd: 0.0045,
  cassette_path: "cassettes/refund_policy_valid_cash/gemini-3.6-flash/3.jsonl",
};

const provider: RawSweepProviderResult = {
  label: "gemini-3.6-flash",
  provider: "gemini",
  model: "gemini-3.6-flash",
  batch_id: "batch_20260924T010317Z_0ec0fb15",
  runs: 5,
  passed: 4,
  failed: 1,
  incomplete: 0,
  not_run: 0,
  flipped_tasks: 1,
  verified_failures: 1,
  cost_usd: 0.0225,
  cost_recorded: 5,
};

const raw: RawSweepSummary = {
  schema_version: SWEEP_SUMMARY_SCHEMA_VERSION,
  sweep_id: "sweep_20260924T010317Z_5c1d2e3f",
  sweep_name: "refund_v0_live",
  spec_path: "fixtures/sweeps/refund_v0_live.json",
  suite_id: "refund_v0",
  started_at: "2026-09-24T01:03:17Z",
  finished_at: "2026-09-24T01:04:02Z",
  seeds: [1, 2, 3, 4, 5],
  task_count: 1,
  providers: [provider],
  tasks: [row],
  flipped_tasks: 1,
  runs: 5,
  cost_usd: 0.0225,
  cost_recorded: 5,
  verified_failures: 1,
  natural_verified_failures: 1,
  cost_per_verified_failure: 0.0225,
  cost_per_natural_verified_failure: 0.0225,
  failing_cells: [cell],
  budget: {
    max_cost_usd: 10,
    spent_usd: 0.0225,
    stop_reason: null,
    detail: null,
    not_run: [],
  },
};

describe("parseSweepSummary", () => {
  it("mirrors the backend's schema version and labels", () => {
    expect(SWEEP_SUMMARY_SCHEMA_VERSION).toBe(
      BACKEND.match(/^SWEEP_SUMMARY_SCHEMA_VERSION = "([^"]+)"$/m)?.[1],
    );
    expect(FAILURE_LABELS).toEqual(
      BACKEND.match(/^FailureLabel = Literal\[(.+)\]$/m)?.[1]
        .split(", ")
        .map((label) => JSON.parse(label)),
    );
  });

  it.each([
    ["SweepSummary", raw],
    ["SweepProviderResult", provider],
    ["SweepTaskRow", row],
    ["SweepFailingCell", cell],
  ] as const)("declares every field of %s", (className, sample) => {
    expect(Object.keys(sample)).toEqual(pythonFields(className));
  });

  it("camelizes the fields a page would read", () => {
    const summary = parseSweepSummary(raw);

    expect(summary.costPerVerifiedFailure).toBe(0.0225);
    expect(summary.providers[0].flippedTasks).toBe(1);
    expect(summary.tasks[0].notRun).toBe(0);
    expect(summary.failingCells[0]).toMatchObject({
      label: "natural",
      naturalCheckIds: ["expected_refund_missing"],
      failedCheckIds: ["expected_refund_missing"],
    });
    expect(summary.budget?.maxCostUsd).toBe(10);
  });
});
