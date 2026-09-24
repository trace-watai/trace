import { describe, expect, it } from "vitest";

import {
  binomialCdf,
  clopperPearsonUpper,
  describeOverBlocking,
  roundUp,
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

  it.each([0, 1, -0.5, 1.5, Number.NaN])(
    "refuses confidence %f outside (0, 1)",
    (confidence) => {
      expect(() => clopperPearsonUpper(0, 1, confidence)).toThrow(RangeError);
    },
  );
});

describe("roundUp", () => {
  it.each([
    [0, 59, 0.0496],
    [0, 58, 0.0504],
    [2, 3, 0.9831],
    [0, 1, 0.95],
    [1, 1, 1],
  ])(
    "stores %i of %i at %f, never below the exact bound",
    (failures, trials, stored) => {
      const exact = clopperPearsonUpper(failures, trials) as number;
      expect(roundUp(exact)).toBe(stored);
      expect(stored).toBeGreaterThanOrEqual(exact - 1e-12);
    },
  );

  it("keeps a value that is already on a step", () => {
    expect(roundUp(0.0051)).toBe(0.0051);
    for (let k = 0; k <= 10_000; k += 1) {
      expect(roundUp(k / 10_000)).toBe(k / 10_000);
    }
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
      "0 of 1 families failed, true rate could be up to 95.00%",
    );
  });

  it.each([
    [59, 0.0496, "4.96%"],
    [58, 0.0504, "5.04%"],
  ])(
    "prints 0 of %i families apart from its neighbor",
    (families, bound, printed) => {
      expect(
        describeOverBlocking({
          ...base,
          independentFamilies: families,
          upperBound95: bound,
        }),
      ).toBe(
        `0 of ${families} families failed, true rate could be up to ${printed}`,
      );
    },
  );

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
