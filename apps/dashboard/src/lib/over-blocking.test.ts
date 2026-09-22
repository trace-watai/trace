import { describe, expect, it } from "vitest";

import {
  binomialCdf,
  clopperPearsonUpper,
  describeOverBlocking,
} from "@/lib/over-blocking";
import type { OverBlocking } from "@/types/metrics-snapshot";

describe("clopperPearsonUpper", () => {
  it.each([
    [0, 1, 0.95],
    [0, 40, 0.0722],
    [1, 40, 0.1132],
    [3, 10, 0.6066],
  ])("bounds %i of %i at %f", (failures, trials, expected) => {
    expect(clopperPearsonUpper(failures, trials)).toBeCloseTo(expected, 4);
  });

  it("needs 59 clean trials to bound under five percent", () => {
    expect(clopperPearsonUpper(0, 59)).toBeLessThan(0.05);
    expect(clopperPearsonUpper(0, 58)).toBeGreaterThan(0.05);
  });

  it("puts the observation at five percent probability", () => {
    const bound = clopperPearsonUpper(1, 40) as number;
    expect(binomialCdf(1, 40, bound)).toBeCloseTo(0.05, 9);
  });

  it("bounds at one when every trial failed and at null over nothing", () => {
    expect(clopperPearsonUpper(3, 3)).toBe(1);
    expect(clopperPearsonUpper(0, 0)).toBeNull();
  });

  it("refuses more failures than trials", () => {
    expect(() => clopperPearsonUpper(2, 1)).toThrow(RangeError);
  });
});

describe("describeOverBlocking", () => {
  const base: OverBlocking = {
    siblingsRun: 1,
    siblingsFailed: 0,
    rate: { numerator: 0, denominator: 1 },
    independentFamilies: 1,
    familiesFailed: 0,
    upperBound95: 0.95,
    sources: [],
  };

  it("states families and how high the true rate could be", () => {
    expect(describeOverBlocking(base)).toBe(
      "0 of 1 families failed, true rate could be up to 95.0%",
    );
  });

  it("says when a record predates family counts", () => {
    expect(
      describeOverBlocking({
        ...base,
        independentFamilies: null,
        familiesFailed: null,
        upperBound95: null,
      }),
    ).toBe("0/1 siblings failed, families not recorded");
  });

  it("says when no family completed", () => {
    expect(
      describeOverBlocking({
        ...base,
        independentFamilies: 0,
        familiesFailed: 0,
        upperBound95: null,
      }),
    ).toBe("nothing measured, no sibling family completed");
  });
});
