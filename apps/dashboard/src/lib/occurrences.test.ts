import { describe, expect, it } from "vitest";

import { occurrenceRows } from "@/lib/occurrences";
import type { BundleOccurrence } from "@/types/failure-card";

const occurrence = (
  runId: string,
  overrides: Partial<BundleOccurrence> = {},
): BundleOccurrence => ({
  runId,
  taskId: "refund_policy_failure",
  provider: "fixture",
  model: "scripted:refund_policy_failure_script",
  seed: null,
  ...overrides,
});

describe("occurrenceRows", () => {
  it("marks the first occurrence and the reproductions after it", () => {
    const rows = occurrenceRows([occurrence("run_a"), occurrence("run_b")]);

    expect(rows.map((row) => [row.runId, row.role])).toEqual([
      ["run_a", "first"],
      ["run_b", "reproduction"],
    ]);
  });

  it("marks the run being viewed, and no run when none is given", () => {
    const occurrences = [occurrence("run_a"), occurrence("run_b")];

    expect(
      occurrenceRows(occurrences, "run_b").map((row) => row.isCurrent),
    ).toEqual([false, true]);
    expect(occurrenceRows(occurrences).some((row) => row.isCurrent)).toBe(
      false,
    );
  });

  it("describes the configuration from what was recorded", () => {
    const [full, bare] = occurrenceRows([
      occurrence("run_a", { provider: "openai", model: "gpt-x", seed: 0 }),
      occurrence("run_b", { provider: null, model: null, seed: null }),
    ]);

    expect(full.configuration).toBe("openai · gpt-x · seed 0");
    expect(bare.configuration).toBeNull();
  });
});
