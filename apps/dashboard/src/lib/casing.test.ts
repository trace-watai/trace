import { describe, expect, expectTypeOf, it } from "vitest";
import { camelizeKeys, type Camelize } from "@/lib/casing";

describe("camelizeKeys", () => {
  it("converts snake_case keys to camelCase", () => {
    expect(camelizeKeys({ root_cause_step: 3 })).toEqual({ rootCauseStep: 3 });
  });

  it("handles multi-underscore keys", () => {
    expect(camelizeKeys({ first_irreversible_action_step: 5 })).toEqual({
      firstIrreversibleActionStep: 5,
    });
  });

  it("leaves already-camelCase and single-word keys untouched", () => {
    expect(camelizeKeys({ confidence: 0.8, runId: "r1" })).toEqual({
      confidence: 0.8,
      runId: "r1",
    });
  });

  it("rewrites keys only, never string values", () => {
    // Enum values like FailureCategory are wire data and must pass through.
    expect(
      camelizeKeys({ primary_failure_category: "stale_source_authority" }),
    ).toEqual({ primaryFailureCategory: "stale_source_authority" });
  });

  it("recurses into nested objects", () => {
    expect(camelizeKeys({ outer_key: { inner_key: 1 } })).toEqual({
      outerKey: { innerKey: 1 },
    });
  });

  it("recurses into arrays of objects", () => {
    expect(
      camelizeKeys({ visible_symptom_steps: [{ step_id: 5 }, { step_id: 6 }] }),
    ).toEqual({ visibleSymptomSteps: [{ stepId: 5 }, { stepId: 6 }] });
  });

  it("preserves primitives and null", () => {
    expect(camelizeKeys(null)).toBeNull();
    expect(camelizeKeys(42)).toBe(42);
    expect(camelizeKeys("a_b")).toBe("a_b");
    expect(camelizeKeys({ maybe_value: null })).toEqual({ maybeValue: null });
  });

  it("preserves array order and primitive array elements", () => {
    expect(camelizeKeys({ evidence_step_ids: [3, 1, 2] })).toEqual({
      evidenceStepIds: [3, 1, 2],
    });
  });

  it("does not mutate the input", () => {
    const input = { root_cause_step: 3 };
    camelizeKeys(input);
    expect(input).toEqual({ root_cause_step: 3 });
  });

  it("maps the wire type to the camelCase domain type", () => {
    type Raw = { run_id: string; root_cause_step: number | null };
    expectTypeOf<Camelize<Raw>>().toEqualTypeOf<{
      runId: string;
      rootCauseStep: number | null;
    }>();
  });
});
