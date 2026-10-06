import { describe, expect, it } from "vitest";

import {
  REGRESSION_SCHEMA_VERSION,
  parseRegressionArtifact,
  type RawRegressionArtifact,
} from "@/types/regression-artifact";

const raw: RawRegressionArtifact = {
  schema_version: "0.2.0",
  test_name: "pins normalized model actions",
  source_run_id: "run_test",
  task_fixture: "fixtures/tasks/refund_policy_failure.json",
  initial_state: {},
  pinned_docs: [],
  pinned_agent_actions: [
    {
      action_type: "tool_call",
      tool_name: "get_order",
      arguments: { customer_name: "Casey Nguyen" },
    },
  ],
  expected_behavior: [],
  forbidden_actions: [],
  verifier_checks: ["unauthorized_cash_refund"],
  positive_sibling_tests: [],
  severity: "critical",
  blocks_release: true,
  replay_command: "trace-harness replay artifact.json",
  metadata: {},
};

describe("parseRegressionArtifact", () => {
  it("defaults legacy artifacts to unlabeled and preserves unknown fields", () => {
    const parsed = parseRegressionArtifact({
      ...raw,
      future_field: { some_value: 1 },
    } as RawRegressionArtifact);
    expect(parsed.replayMode).toBe("unlabeled");
    expect(parsed.replayModeBasis).toBeNull();
    expect(parsed).toHaveProperty("futureField", { someValue: 1 });
  });
  it("matches the executable pinned-input backend schema", () => {
    expect(REGRESSION_SCHEMA_VERSION).toBe("0.3.0");
    expect(parseRegressionArtifact(raw).pinnedAgentActions).toEqual([
      {
        actionType: "tool_call",
        toolName: "get_order",
        arguments: { customerName: "Casey Nguyen" },
      },
    ]);
  });
});
