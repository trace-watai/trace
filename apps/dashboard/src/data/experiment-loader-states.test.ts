import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  ExperimentNotFoundError,
  getExperiment,
  listExperiments,
  listUnreadableExperiments,
} from "@/data/experiment-loader";
import { MalformedArtifactError } from "@/data/run-store";
import {
  EXPERIMENT_ID_PATTERN,
  EXPERIMENT_METRIC_NAMES,
  type RawExperimentResult,
  type RawExperimentSpec,
} from "@/types/experiment";

const REPO_ROOT = path.join(process.cwd(), "..", "..");

let runsDir: string;

beforeEach(() => {
  runsDir = mkdtempSync(path.join(tmpdir(), "trace-experiments-"));
  process.env.TRACE_RUNS_DIR = runsDir;
});

afterEach(() => {
  rmSync(runsDir, { recursive: true, force: true });
  delete process.env.TRACE_RUNS_DIR;
});

const rawSpec = (experimentId: string): RawExperimentSpec => ({
  schema_version: "0.1.0",
  experiment_id: experimentId,
  brief_path: null,
  hypothesis: "the control stops the out-of-window cash refund",
  frozen_manifest: {
    suite_id: "refund_bundles_v0",
    verifier_ids: ["refund_policy"],
    fixtures_hash: "sha256:deadbeef",
  },
  conditions: [
    {
      name: "live_on",
      kind: "live",
      agent_config: {
        label: "gemini_live",
        provider: "gemini",
        model: "gemini-3.6-flash",
        max_steps: 16,
        timeout_seconds: 120,
        cassette: { mode: "replay", directory: "fixtures/cassettes" },
      },
      control_ids: ["ctl_refund_window_v1"],
      seeds: [0, 1],
      start: { source_run_id: "run_x", step_id: 2 },
    },
  ],
  budget: { max_runs: 10, max_cost_usd: 1 },
  created_at: "2026-01-01T00:00:00Z",
  metadata: {},
});

const rawResult = (experimentId: string): RawExperimentResult => ({
  schema_version: "0.1.0",
  experiment_id: experimentId,
  condition_batches: { live_on: "batch_on", replay_only: "batch_replay" },
  metrics: {
    verified_failure_count: 3,
    post_block_outcomes: { substitute_violation: 2, no_block_observed: 1 },
    extra: {
      cost_recorded_k: 9,
      "latency_ms_p50.live_on": 900,
    },
  },
  decision: "review",
  decided_by: "human",
  report_path: null,
  finished_at: "2026-01-01T01:00:00Z",
  metadata: {},
});

const write = (experimentId: string, files: Record<string, unknown>) => {
  const dir = path.join(runsDir, "experiments", experimentId);
  mkdirSync(dir, { recursive: true });
  for (const [name, body] of Object.entries(files)) {
    writeFileSync(
      path.join(dir, name),
      typeof body === "string" ? body : JSON.stringify(body),
    );
  }
};

describe("experiment loader states", () => {
  it("keeps data keys byte for byte and camelizes field names", () => {
    write("exp_a", {
      "experiment.json": rawSpec("exp_a"),
      "result.json": rawResult("exp_a"),
    });
    const { spec, result } = getExperiment("exp_a");

    expect(spec.conditions[0].agentConfig.cassette).toEqual({
      mode: "replay",
      directory: "fixtures/cassettes",
    });
    expect(spec.conditions[0].controlIds).toEqual(["ctl_refund_window_v1"]);
    expect(result?.conditionBatches).toEqual({
      live_on: "batch_on",
      replay_only: "batch_replay",
    });
    expect(result?.metrics.postBlockOutcomes).toEqual({
      substitute_violation: 2,
      no_block_observed: 1,
    });
    expect(result?.metrics.extra).toEqual({
      cost_recorded_k: 9,
      "latency_ms_p50.live_on": 900,
    });
    expect(result?.metrics.verifiedFailureCount).toBe(3);
  });

  it("reads a plan with no result yet as a null result", () => {
    write("exp_planned", { "experiment.json": rawSpec("exp_planned") });
    expect(getExperiment("exp_planned").result).toBeNull();
  });

  it("throws not-found for an unknown id and for one that leaves the directory", () => {
    write("exp_a", { "experiment.json": rawSpec("exp_a") });
    // A plan one level up that a traversing id would otherwise reach.
    writeFileSync(
      path.join(runsDir, "experiment.json"),
      JSON.stringify(rawSpec("exp_a")),
    );
    for (const id of ["exp_missing", "..", "../experiments/exp_a", "exp.a"]) {
      expect(() => getExperiment(id)).toThrow(ExperimentNotFoundError);
    }
  });

  it("does not let one unreadable experiment hide the others", () => {
    write("exp_good", {
      "experiment.json": rawSpec("exp_good"),
      "result.json": rawResult("exp_good"),
    });
    write("exp_broken", { "experiment.json": "{" });
    write("exp_copied", { "experiment.json": rawSpec("exp_good") });
    write("exp_bad_result", {
      "experiment.json": rawSpec("exp_bad_result"),
      "result.json": "not json",
    });

    expect(listExperiments().map((s) => s.experimentId)).toEqual(["exp_good"]);
    expect(
      listUnreadableExperiments().map((entry) => entry.experimentId),
    ).toEqual(["exp_bad_result", "exp_broken", "exp_copied"]);
    expect(() => getExperiment("exp_broken")).toThrow(MalformedArtifactError);
    expect(() => getExperiment("exp_copied")).toThrow(/names experiment/);
  });
});

describe("experiment mirror", () => {
  it("lists the memo's metric names in order", () => {
    const memo = readFileSync(
      path.join(REPO_ROOT, "docs", "methodology_metrics.md"),
      "utf8",
    );
    const block = /## Appendix\. Experiment metric names[\s\S]*?```\n([\s\S]*?)```/.exec(
      memo,
    );
    expect(block).not.toBeNull();
    const camel = (name: string) =>
      name.replace(/_([a-z0-9])/g, (_, c: string) => c.toUpperCase());
    const names = (block?.[1] ?? "")
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean)
      .map(camel);
    expect([...EXPERIMENT_METRIC_NAMES]).toEqual(names);
  });

  it("uses the backend's experiment id pattern", () => {
    const source = readFileSync(
      path.join(REPO_ROOT, "src", "trace_harness", "runner", "experiment.py"),
      "utf8",
    );
    const python = /EXPERIMENT_ID_PATTERN = r"([^"]+)"/.exec(source);
    expect(python?.[1]).toBe(EXPERIMENT_ID_PATTERN.source);
  });
});
