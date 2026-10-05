import { describe, expect, it } from "vitest";

import { loadRefundFailureFixture } from "@/data/refund-failure-fixture";
import { resolveCheckIndex, sortedFailedChecks } from "@/lib/verifier-checks";
import type { FailedCheck } from "@/types/verifier-result";

const check = (checkId: string, severity: FailedCheck["severity"]) =>
  ({ checkId, severity }) as FailedCheck;

describe("sortedFailedChecks", () => {
  it("puts the critical check first and keeps the verifier's order within a tier", () => {
    const { failedChecks } = loadRefundFailureFixture().verifierResult;

    expect(sortedFailedChecks(failedChecks).map((c) => c.checkId)).toEqual([
      "unauthorized_cash_refund",
      "required_escalation_missing",
      "ticket_outage_claim_unsupported",
      "deprecated_policy_treated_as_authoritative",
    ]);
  });

  it("keeps input order for equal severities", () => {
    const sorted = sortedFailedChecks([
      check("b", "medium"),
      check("a", "medium"),
      check("c", "medium"),
    ]);

    expect(sorted.map((c) => c.checkId)).toEqual(["b", "a", "c"]);
  });

  it("does not mutate its input", () => {
    const input = [check("low", "low"), check("crit", "critical")];

    sortedFailedChecks(input);

    expect(input.map((c) => c.checkId)).toEqual(["low", "crit"]);
  });

  it("returns an empty list for no checks", () => {
    expect(sortedFailedChecks([])).toEqual([]);
  });
});

describe("resolveCheckIndex", () => {
  const checks = [check("a", "high"), check("b", "medium"), check("c", "low")];

  it("finds the requested check", () => {
    expect(resolveCheckIndex(checks, "b")).toBe(1);
  });

  it("falls back to the first check when the id is missing or unknown", () => {
    expect(resolveCheckIndex(checks, undefined)).toBe(0);
    expect(resolveCheckIndex(checks, "nope")).toBe(0);
  });
});
