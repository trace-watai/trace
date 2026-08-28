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
    });
  });
});
