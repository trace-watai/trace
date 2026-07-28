import { describe, expect, it } from "vitest";

import { severityBadgeClasses, severityLabel } from "@/lib/severity-style";
import { SEVERITIES } from "@/types/severity";

describe("severityLabel", () => {
  it("capitalizes the severity name", () => {
    expect(severityLabel("low")).toBe("Low");
    expect(severityLabel("critical")).toBe("Critical");
  });
});

describe("severityBadgeClasses", () => {
  it("returns a non-empty class string for every severity", () => {
    for (const severity of SEVERITIES) {
      expect(severityBadgeClasses(severity)).toMatch(/\S/);
    }
  });

  it("gives each severity a distinct set of classes", () => {
    const classes = SEVERITIES.map(severityBadgeClasses);
    expect(new Set(classes).size).toBe(SEVERITIES.length);
  });
});
