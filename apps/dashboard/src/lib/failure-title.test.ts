import { describe, expect, it } from "vitest";

import { briefFailureTitle } from "@/lib/failure-title";

describe("briefFailureTitle", () => {
  it("keeps only the task title before the headline separator", () => {
    expect(
      briefFailureTitle(
        "Refund request at 47 days (no approval, no outage): cash refund REF-0001 ($432.00) issued at 47 days without manager approval",
      ),
    ).toBe("Refund request at 47 days (no approval, no outage)");
  });

  it("splits on the first separator only", () => {
    expect(briefFailureTitle("A: B: C")).toBe("A");
  });

  it("falls back to the full title when there is no separator", () => {
    expect(briefFailureTitle("Short title")).toBe("Short title");
  });
});
