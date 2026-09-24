import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  getAttribution,
  getBundle,
  getRun,
  getTask,
  getVerifier,
  listRuns,
  MalformedArtifactError,
  RunNotFoundError,
} from "@/data/run-loader";

let runsDir: string;

beforeEach(() => {
  runsDir = mkdtempSync(path.join(tmpdir(), "trace-runs-"));
  process.env.TRACE_RUNS_DIR = runsDir;
});

afterEach(() => {
  rmSync(runsDir, { recursive: true, force: true });
  delete process.env.TRACE_RUNS_DIR;
});

const writeRun = (runId: string, files: Record<string, unknown>): void => {
  const dir = path.join(runsDir, runId);
  mkdirSync(dir, { recursive: true });
  for (const [name, content] of Object.entries(files)) {
    const body =
      typeof content === "string" ? content : JSON.stringify(content);
    writeFileSync(path.join(dir, name), body);
  }
};

const rawRunResult = (overrides: Partial<Record<string, unknown>> = {}) => ({
  schema_version: "0.1.0",
  run_id: "run_1",
  task_id: "task_1",
  status: "completed",
  termination_reason: "final_answer",
  steps_taken: 3,
  final_output: null,
  artifact_paths: {},
  started_at: "2026-01-01T00:00:00Z",
  finished_at: "2026-01-01T00:00:01Z",
  error: null,
  ...overrides,
});

describe("run-loader", () => {
  it("reads run + task for an existing run", () => {
    writeRun("run_1", {
      "run_result.json": rawRunResult(),
      "task_spec.json": { task_id: "task_1", title: "A task", goal: "Do it" },
    });

    const run = getRun("run_1");
    expect(run.runId).toBe("run_1");
    expect(run.terminationReason).toBe("final_answer");

    const task = getTask("run_1");
    expect(task).toEqual({ taskId: "task_1", title: "A task", goal: "Do it" });
  });

  it("throws RunNotFoundError for an unknown run id", () => {
    expect(() => getRun("does_not_exist")).toThrow(RunNotFoundError);
    expect(() => getTask("does_not_exist")).toThrow(RunNotFoundError);
    expect(() => getBundle("does_not_exist")).toThrow(RunNotFoundError);
  });

  it("returns null for downstream artifacts not yet produced", () => {
    writeRun("run_1", { "run_result.json": rawRunResult() });

    expect(getVerifier("run_1")).toBeNull();
    expect(getAttribution("run_1")).toBeNull();
    expect(getBundle("run_1")).toBeNull();
  });

  it("returns the bundle once all three bundle artifacts exist", () => {
    writeRun("run_1", {
      "run_result.json": rawRunResult(),
      "failure_card.json": {
        schema_version: "0.4.0",
        run_id: "run_1",
        task_id: "task_1",
        title: "Something broke",
        summary: "summary",
        task_result: "failed",
        severity: "high",
        root_cause: "cause",
        contributing_failures: ["planning_error"],
        step_ids: [1],
        visible_symptoms: [],
        evidence: [],
        causal_explanation: "explanation",
        blast_radius: {
          refund_count: 1,
          refund_total_usd: 10,
          ticket_count: 0,
          escalation_count: 0,
          customers_affected: [],
          summary: "$10 refunded",
        },
        metadata: {},
      },
      "repair_package.json": { schema_version: "0.3.0", controls: [] },
      "regression_artifact.json": { schema_version: "0.2.0" },
    });

    const bundle = getBundle("run_1");
    expect(bundle).not.toBeNull();
    expect(bundle?.failureCard.blastRadius.summary).toBe("$10 refunded");
  });

  it("throws MalformedArtifactError for invalid JSON", () => {
    writeRun("run_1", { "run_result.json": "{ not valid json" });

    expect(() => getRun("run_1")).toThrow(MalformedArtifactError);
  });

  it("lists runs from the index, and returns an empty list with no index", () => {
    expect(listRuns()).toEqual([]);

    writeFileSync(
      path.join(runsDir, "index.json"),
      JSON.stringify({
        schema_version: "0.2.0",
        entries: [
          {
            run_id: "run_1",
            task_id: "task_1",
            status: "completed",
            termination_reason: "final_answer",
            steps_taken: 3,
            started_at: "2026-01-01T00:00:00Z",
            finished_at: "2026-01-01T00:00:01Z",
            error: null,
            verifier_passed: false,
            failed_check_count: 2,
          },
        ],
      }),
    );

    const runs = listRuns();
    expect(runs).toHaveLength(1);
    expect(runs[0]).toMatchObject({
      runId: "run_1",
      verifierPassed: false,
      failedCheckCount: 2,
      batchId: null,
      bundleKey: null,
    });
  });

  describe("one card per root cause (#211)", () => {
    const KEY = "v1:unsafe_irreversible_action:issue_refund:e621a26e69c17400";
    const occurrence = (runId: string, seed: number) => ({
      run_id: runId,
      task_id: "task_1",
      provider: "fixture",
      model: "scripted:x",
      seed,
    });
    const card = {
      schema_version: "0.5.0",
      run_id: "run_1",
      task_id: "task_1",
      title: "Something broke",
      summary: "summary",
      task_result: "failed",
      severity: "high",
      root_cause: "cause",
      contributing_failures: ["unsafe_irreversible_action"],
      step_ids: [3],
      visible_symptoms: [],
      evidence: [],
      causal_explanation: "explanation",
      blast_radius: {
        refund_count: 1,
        refund_total_usd: 10,
        ticket_count: 0,
        escalation_count: 0,
        customers_affected: [],
        summary: "$10 refunded",
      },
      metadata: {},
      bundle_key: KEY,
      occurrences: [occurrence("run_1", 1), occurrence("run_2", 2)],
    };
    const pointer = (canonical: string) => ({
      schema_version: "0.1.0",
      run_id: "run_2",
      task_id: "task_1",
      bundle_key: KEY,
      canonical_run_id: canonical,
    });

    it("serves a reproduction the card it joined", () => {
      writeRun("run_1", {
        "run_result.json": rawRunResult(),
        "failure_card.json": card,
        "repair_package.json": { schema_version: "0.3.0", controls: [] },
        "regression_artifact.json": {
          schema_version: "0.3.0",
          source_run_id: "run_1",
        },
      });
      writeRun("run_2", {
        "run_result.json": rawRunResult({ run_id: "run_2" }),
        "bundle_ref.json": pointer("run_1"),
      });

      const bundle = getBundle("run_2");

      expect(bundle?.failureCard.runId).toBe("run_1");
      expect(bundle?.failureCard.bundleKey).toBe(KEY);
      expect(
        bundle?.failureCard.occurrences.map((o) => [o.runId, o.seed]),
      ).toEqual([
        ["run_1", 1],
        ["run_2", 2],
      ]);
      expect(bundle?.regressionArtifact.sourceRunId).toBe("run_1");
      expect(getBundle("run_1")).toEqual(bundle);
    });

    it.each(["../outside", "a/b", "..", "C:run", ""])(
      "refuses a pointer naming %j",
      (canonical) => {
        writeRun("run_2", {
          "run_result.json": rawRunResult({ run_id: "run_2" }),
          "bundle_ref.json": pointer(canonical),
        });

        expect(() => getBundle("run_2")).toThrow(MalformedArtifactError);
      },
    );

    it.each(["null", "[]", '"run_1"', "7"])(
      "refuses a pointer file holding %s",
      (content) => {
        writeRun("run_1", { "run_result.json": rawRunResult() });
        writeRun("run_2", {
          "run_result.json": rawRunResult({ run_id: "run_2" }),
          "bundle_ref.json": content,
        });

        expect(() => getBundle("run_2")).toThrow(MalformedArtifactError);
      },
    );

    it("refuses a pointer to a run missing from the runs dir", () => {
      writeRun("run_2", {
        "run_result.json": rawRunResult({ run_id: "run_2" }),
        "bundle_ref.json": pointer("run_1"),
      });

      expect(() => getBundle("run_2")).toThrow(MalformedArtifactError);
      expect(() => getBundle("run_2")).toThrow(/run_1, which is not in/);
    });

    it("lists each run's bundle key from an index at 0.6.0", () => {
      writeFileSync(
        path.join(runsDir, "index.json"),
        JSON.stringify({
          schema_version: "0.6.0",
          entries: ["run_1", "run_2"].map((runId) => ({
            run_id: runId,
            task_id: "task_1",
            status: "completed",
            termination_reason: "final_answer",
            steps_taken: 3,
            started_at: "2026-01-01T00:00:00Z",
            finished_at: "2026-01-01T00:00:01Z",
            error: null,
            verifier_passed: false,
            failed_check_count: 1,
            bundle_key: KEY,
          })),
        }),
      );

      expect(listRuns().map((run) => run.bundleKey)).toEqual([KEY, KEY]);
    });
  });
});
