import { describe, expect, it } from "vitest";

import { loadRefundFailureFixture } from "@/data/refund-failure-fixture";

const ARTIFACT_NAMES = [
  "task_spec.json",
  "run_config.json",
  "initial_state.json",
  "final_state.json",
  "trace.jsonl",
  "run_result.json",
  "verifier_result.json",
  "attribution_result.json",
  "failure_card.json",
  "repair_package.json",
  "regression_artifact.json",
] as const;

describe("loadRefundFailureFixture", () => {
  it("loads one coherent generated run across all eleven artifacts", () => {
    const fixture = loadRefundFailureFixture();
    const runId = fixture.runResult.runId;

    expect(
      new Set([
        fixture.runResult.runId,
        fixture.verifierResult.runId,
        fixture.attributionResult.runId,
        fixture.failureCard.runId,
        fixture.repairPackage.runId,
        fixture.regressionArtifact.sourceRunId,
        ...fixture.trace.map((event) => event.runId),
      ]),
    ).toEqual(new Set([runId]));
    expect(fixture.artifactNames).toEqual(ARTIFACT_NAMES);
    expect(fixture.taskSpec).toMatchObject({
      task_id: "refund_policy_failure",
    });
    expect(fixture.runConfig).toMatchObject({
      task_id: "refund_policy_failure",
    });
    expect(fixture.initialState).toHaveProperty("orders");
    expect(fixture.finalState).toHaveProperty("refunds");
  });

  it("exposes the authoritative run and verifier summary", () => {
    const fixture = loadRefundFailureFixture();

    expect(fixture.runResult).toMatchObject({
      taskId: "refund_policy_failure",
      status: "completed",
      terminationReason: "final_answer",
      stepsTaken: 7,
    });
    expect(fixture.verifierResult).toMatchObject({
      passed: false,
      severity: "critical",
      blocksRelease: true,
    });
    expect(fixture.trace.at(0)?.eventType).toBe("run_started");
    expect(fixture.trace.at(-1)?.eventType).toBe("run_finished");
  });

  it("resolves every linked verifier and failure-card step into the trace", () => {
    const fixture = loadRefundFailureFixture();
    const traceSteps = new Set(
      fixture.trace.flatMap((event) =>
        event.stepId === null ? [] : [event.stepId],
      ),
    );
    const linkedSteps = [
      ...fixture.verifierResult.failedChecks.flatMap((check) => [
        ...check.stepIds,
        ...check.evidence.flatMap((evidence) => evidence.stepIds),
      ]),
      ...fixture.failureCard.stepIds,
      ...fixture.failureCard.evidence.flatMap((evidence) => evidence.stepIds),
    ];

    expect(linkedSteps.length).toBeGreaterThan(0);
    expect(linkedSteps.every((stepId) => traceSteps.has(stepId))).toBe(true);
  });
});
