import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

import { parseBatchSummary } from "@/types/batch-summary";
import {
  EXPERIMENT_SCHEMA_VERSION,
  parseExperimentSpec,
  type RawAgentConfig,
  type RawCallPolicy,
  type RawCassetteConfig,
} from "@/types/experiment";

const REPO = path.join(process.cwd(), "..", "..");

/** The annotated fields of one Pydantic model, read from the backend source. */
const pythonFields = (file: string, model: string): string[] => {
  const source = readFileSync(path.join(REPO, file), "utf8");
  const body = source
    .split(`class ${model}(BaseModel):`)[1]
    .split("\nclass ")[0];
  return [...body.matchAll(/^ {4}(\w+): /gm)].map((match) => match[1]);
};

const CASSETTE: Required<RawCassetteConfig> = {
  mode: "record",
  directory: "cassettes/live",
};

const CALL_POLICY: Required<RawCallPolicy> = {
  max_attempts: 5,
  initial_delay_seconds: 2,
  max_delay_seconds: 30,
  backoff_multiplier: 2,
  jitter: true,
  requests_per_minute: 10,
};

/** Every field, so the type checker fails when the mirror gains or loses one. */
const AGENT: Required<RawAgentConfig> = {
  label: "gemini",
  provider: "gemini",
  model: "gemini-3.6-flash",
  prompt_version: null,
  temperature: null,
  seed: null,
  max_steps: 16,
  timeout_seconds: 120,
  cassette: CASSETTE,
  call_policy: CALL_POLICY,
};

describe("the agent config mirror", () => {
  it.each([
    ["src/trace_harness/runner/suite.py", "AgentConfig", AGENT],
    ["src/trace_harness/models/cassette.py", "CassetteConfig", CASSETTE],
    ["src/trace_harness/models/policy.py", "CallPolicy", CALL_POLICY],
  ])("has every field of %s %s", (file, model, mirror) => {
    expect(Object.keys(mirror).sort()).toEqual(
      pythonFields(file, model).sort(),
    );
  });

  it("carries the cassette and call policy through a plan and a batch", () => {
    const spec = parseExperimentSpec({
      schema_version: EXPERIMENT_SCHEMA_VERSION,
      experiment_id: "exp_x",
      hypothesis: "h",
      frozen_manifest: {
        suite_id: "refund_v0",
        verifier_ids: [],
        fixtures_hash: "sha256:x",
      },
      conditions: [
        {
          name: "live",
          kind: "live",
          agent_config: AGENT,
          control_ids: [],
          seeds: [0],
        },
      ],
      budget: { max_runs: 5, max_cost_usd: 1 },
      created_at: "2026-01-01T00:00:00Z",
      metadata: {},
    });
    const batch = parseBatchSummary({
      schema_version: "0.4.0",
      batch_id: "batch_x",
      suite_id: "refund_v0",
      started_at: "2026-01-01T00:00:00Z",
      finished_at: "2026-01-01T00:00:01Z",
      agent_configs: [AGENT],
      entries: [],
      aggregates: {
        total: 0,
        completed: 0,
        terminated: 0,
        errored: 0,
        verifier_passed: 0,
        verifier_failed: 0,
        cost_recorded: 0,
        known_cost_usd: 0,
        by_agent: {},
      },
    });

    for (const agent of [
      spec.conditions[0].agentConfig,
      batch.agentConfigs[0],
    ]) {
      expect(agent.cassette).toEqual({
        mode: "record",
        directory: "cassettes/live",
      });
      expect(agent.callPolicy).toMatchObject({
        maxAttempts: 5,
        requestsPerMinute: 10,
      });
    }
  });
});
