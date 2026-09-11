import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import { buildTraceSteps, stepTitle, type TraceStep } from "@/lib/trace-steps";
import {
  parseAttributionResult,
  type RawAttributionResult,
} from "@/types/attribution";
import { parseTrace, type RawTraceEvent } from "@/types/trace-event";
import {
  parseVerifierResult,
  type RawVerifierResult,
} from "@/types/verifier-result";

// The same real, pipeline-produced refund-failure bundle the rest of the
// dashboard's contract tests use — guards this against wire-contract drift
// the same way, not just synthetic shapes.
import rawAttribution from "@/fixtures/refund-failure/attribution_result.json";
import rawVerifier from "@/fixtures/refund-failure/verifier_result.json";

const fixtureUrl = (name: string): URL =>
  new URL(`../fixtures/refund-failure/${name}`, import.meta.url);

const loadTrace = () =>
  parseTrace(
    readFileSync(fixtureUrl("trace.jsonl"), "utf8")
      .trim()
      .split("\n")
      .map((line) => JSON.parse(line) as RawTraceEvent),
  );

const attribution = parseAttributionResult(
  rawAttribution as unknown as RawAttributionResult,
);
const verifier = parseVerifierResult(
  rawVerifier as unknown as RawVerifierResult,
);

const stepById = (steps: TraceStep[], stepId: number): TraceStep => {
  const step = steps.find((s) => s.stepId === stepId);
  if (!step) throw new Error(`step ${stepId} not found`);
  return step;
};

describe("buildTraceSteps", () => {
  it("groups the fixture trace into one ordered step per decision", () => {
    const steps = buildTraceSteps(loadTrace(), attribution, verifier);

    expect(steps.map((s) => s.stepId)).toEqual([1, 2, 3, 4, 5, 6, 7]);
  });

  it("attaches a tool-call action and its retrieval results", () => {
    const steps = buildTraceSteps(loadTrace(), attribution, verifier);
    const step1 = stepById(steps, 1);

    expect(step1.action).toEqual({
      kind: "tool_call",
      toolName: "search_docs",
      arguments: { query: "refund policy" },
    });
    expect(stepTitle(step1)).toBe("Called `search_docs`");
    expect(step1.retrievalResults.map((r) => r.docId)).toEqual([
      "refund_policy_v4",
      "refund_policy_v2",
    ]);
    expect(step1.retrievalResults[1]).toMatchObject({ status: "deprecated" });
  });

  it("renders the final step as a final_answer action", () => {
    const steps = buildTraceSteps(loadTrace(), attribution, verifier);
    const finalStep = stepById(steps, 7);

    expect(finalStep.action.kind).toBe("final_answer");
    expect(stepTitle(finalStep)).toBe("Final answer");
  });

  it("attaches attribution markers to the steps they actually happened on, never merged", () => {
    const steps = buildTraceSteps(loadTrace(), attribution, verifier);

    expect(
      stepById(steps, 3)
        .markers.map((m) => m.kind)
        .sort(),
    ).toEqual(["first_bad", "root_cause"].sort());
    expect(stepById(steps, 4).markers.map((m) => m.kind)).toEqual([
      "missed_recovery",
    ]);
    // Step 5 carries three distinct markers simultaneously — the methodology
    // doc's rule is that these stay separate entries, never collapsed.
    expect(
      stepById(steps, 5)
        .markers.map((m) => m.kind)
        .sort(),
    ).toEqual(
      [
        "first_irreversible_action",
        "first_unrecoverable",
        "visible_symptom",
      ].sort(),
    );
    expect(stepById(steps, 6).markers.map((m) => m.kind)).toEqual([
      "visible_symptom",
    ]);
    expect(stepById(steps, 1).markers).toEqual([]);
  });

  it("attaches failed checks by step id", () => {
    const steps = buildTraceSteps(loadTrace(), attribution, verifier);

    expect(
      stepById(steps, 5)
        .failedChecks.map((c) => c.checkId)
        .sort(),
    ).toEqual(
      [
        "deprecated_policy_treated_as_authoritative",
        "unauthorized_cash_refund",
      ].sort(),
    );
    expect(stepById(steps, 1).failedChecks).toEqual([]);
  });

  it("degrades to empty markers and failed checks when attribution/verifier are unavailable", () => {
    const steps = buildTraceSteps(loadTrace(), null, null);

    for (const step of steps) {
      expect(step.markers).toEqual([]);
      expect(step.failedChecks).toEqual([]);
    }
  });

  it("surfaces a run-level error instead of an empty step when the model call itself fails", () => {
    const trace = parseTrace([
      {
        schema_version: "0.3.0",
        event_id: "evt-error-1",
        run_id: "run-failed",
        step_id: 3,
        timestamp: "2026-01-01T00:00:00Z",
        metadata: {},
        parent_event_id: null,
        event_type: "error",
        payload: {
          error: "model call timed out after 30s",
          kind: "model_timeout",
          traceback: null,
        },
      },
    ]);

    const steps = buildTraceSteps(trace, null, null);
    const step = stepById(steps, 3);

    expect(step.action).toEqual({ kind: "none" });
    expect(step.observation).toBeNull();
    expect(step.runError).toEqual({
      kind: "model_timeout",
      message: "model call timed out after 30s",
    });
    expect(stepTitle(step)).toBe("Run error");
  });
});

describe("stepTitle", () => {
  it("falls back to the step number when there is no action", () => {
    const step: TraceStep = {
      stepId: 9,
      reasoning: null,
      action: { kind: "none" },
      observation: null,
      runError: null,
      retrievalResults: [],
      markers: [],
      failedChecks: [],
      events: [],
    };

    expect(stepTitle(step)).toBe("Step 9");
  });
});
