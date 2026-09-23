import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

import {
  BATCH_SUMMARY_SCHEMA_VERSION,
  parseBatchSummary,
  type RawBatchSummary,
} from "@/types/batch-summary";

const REPO = path.join(process.cwd(), "..", "..");
const BATCHES = path.join(REPO, "docs", "acceptance", "batches");

const retained = (): RawBatchSummary => {
  const [batchId] = readdirSync(BATCHES);
  return JSON.parse(
    readFileSync(path.join(BATCHES, batchId, "batch_summary.json"), "utf8"),
  ) as RawBatchSummary;
};

/** The retained 0.2.0 summary rewritten as each schema version wrote it. */
const asVersion = (version: string): RawBatchSummary => {
  const raw = retained();
  if (version === "0.1.0") {
    // 0.1.0 had no verdicts and no incomplete counts.
    for (const entry of raw.entries) delete entry.verdict;
    delete raw.aggregates.incomplete;
    for (const counts of Object.values(raw.aggregates.by_agent)) {
      delete counts.incomplete;
    }
    return { ...raw, schema_version: version };
  }
  const budget = {
    max_cost_usd: 0.01,
    spent_usd: 0.18,
    stop_reason: "budget_exhausted" as const,
    detail: "recorded live spend $0.180000 reached the $0.010000 cap",
    not_run: [
      { agent_label: "claude", task_path: "fixtures/tasks/refund.json" },
    ],
  };
  if (version === "0.3.0") {
    return { ...raw, schema_version: version, budget };
  }
  if (version === "0.4.0") {
    return {
      ...raw,
      schema_version: version,
      budget,
      entries: raw.entries.map((entry, seed) => ({
        ...entry,
        condition: "live",
        seed,
        diverged: false,
        first_post_fork_divergence_step: null,
        post_block_outcome: "no_block_observed" as const,
      })),
      metadata: { experiment_id: "exp_x", condition: "live" },
    };
  }
  return raw;
};

describe("parseBatchSummary", () => {
  it("mirrors the backend's schema version", () => {
    const backend = readFileSync(
      path.join(REPO, "src", "trace_harness", "runner", "batch.py"),
      "utf8",
    ).match(/^BATCH_SUMMARY_SCHEMA_VERSION = "([^"]+)"$/m)?.[1];

    expect(BATCH_SUMMARY_SCHEMA_VERSION).toBe(backend);
  });

  it("reads a summary written before the branch stage", () => {
    const raw = retained();
    const summary = parseBatchSummary(raw);

    expect(raw.schema_version).toBe("0.2.0");
    expect(summary.batchId).toBe(raw.batch_id);
    expect(summary.budget).toBeUndefined();
    expect(summary.metadata).toBeUndefined();
    expect(summary.entries.every((e) => e.diverged === undefined)).toBe(true);
  });

  it.each(["0.1.0", "0.2.0", "0.3.0", "0.4.0"])(
    "reads a %s summary",
    (version) => {
      const raw = asVersion(version);
      const summary = parseBatchSummary(raw);

      expect(summary.schemaVersion).toBe(version);
      expect(summary.entries).toHaveLength(raw.entries.length);
      expect(summary.aggregates.byAgent).toEqual(raw.aggregates.by_agent);
      expect(summary.entries[0].verdict === undefined).toBe(
        version === "0.1.0",
      );
      expect(summary.budget === undefined).toBe(version < "0.3.0");
      expect(summary.metadata === undefined).toBe(version < "0.4.0");
    },
  );

  it("carries the budget block", () => {
    const summary = parseBatchSummary(asVersion("0.3.0"));

    expect(summary.budget).toEqual({
      maxCostUsd: 0.01,
      spentUsd: 0.18,
      stopReason: "budget_exhausted",
      detail: "recorded live spend $0.180000 reached the $0.010000 cap",
      notRun: [
        { agentLabel: "claude", taskPath: "fixtures/tasks/refund.json" },
      ],
    });
  });

  it("carries the branch fields and keeps agent labels intact", () => {
    const summary = parseBatchSummary({
      schema_version: BATCH_SUMMARY_SCHEMA_VERSION,
      batch_id: "batch_x",
      suite_id: "branch",
      started_at: "2026-01-01T00:00:00Z",
      finished_at: "2026-01-01T00:00:01Z",
      agent_configs: [],
      entries: [
        {
          run_id: "run_x",
          task_id: "t",
          task_path: "fixtures/tasks/t.json",
          agent_label: "scripted_store_credit",
          provider: "fixture",
          status: "completed",
          condition: "live",
          seed: 0,
          first_post_fork_divergence_step: 3,
          diverged: true,
          post_block_outcome: "substitute_violation",
        },
      ],
      aggregates: {
        total: 1,
        completed: 1,
        terminated: 0,
        errored: 0,
        verifier_passed: 0,
        verifier_failed: 1,
        cost_recorded: 1,
        known_cost_usd: 0,
        by_agent: { scripted_store_credit: { failed: 1 } },
      },
      metadata: { experiment_id: "exp_x", condition: "live" },
    });

    expect(summary.entries[0]).toMatchObject({
      firstPostForkDivergenceStep: 3,
      diverged: true,
      postBlockOutcome: "substitute_violation",
    });
    expect(summary.aggregates.byAgent).toEqual({
      scripted_store_credit: { failed: 1 },
    });
    expect(summary.metadata).toEqual({
      experimentId: "exp_x",
      condition: "live",
    });
  });
});
