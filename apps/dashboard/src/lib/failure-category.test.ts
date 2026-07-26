import { describe, expect, it } from "vitest";

import { failureCategoryLabel } from "@/lib/failure-category";
import { FAILURE_CATEGORIES } from "@/types/attribution";

describe("failureCategoryLabel", () => {
  it("humanizes a snake_case category", () => {
    expect(failureCategoryLabel("stale_source_authority")).toBe(
      "Stale source authority",
    );
    expect(failureCategoryLabel("unknown")).toBe("Unknown");
  });

  it("produces a non-empty label for every category in the vocabulary", () => {
    for (const category of FAILURE_CATEGORIES) {
      expect(failureCategoryLabel(category)).toMatch(/\S/);
    }
  });
});
