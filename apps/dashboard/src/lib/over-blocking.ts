/**
 * Over-blocking as the page states it: family counts with an upper bound.
 *
 * `clopperPearsonUpper` mirrors `clopper_pearson_upper` in
 * `src/trace_harness/metrics/bounds.py`. The page derives the bound from the
 * two family counts and ignores the serialized number, as `ratioValue` does
 * for a rate.
 */

import type { OverBlocking } from "@/types/metrics-snapshot";

/** `P(X <= k)` for `X ~ Binomial(n, p)`, summed in log space. */
export const binomialCdf = (k: number, n: number, p: number): number => {
  if (p <= 0) return 1;
  if (p >= 1) return k >= n ? 1 : 0;
  const logP = Math.log(p);
  const logQ = Math.log1p(-p);
  let logChoose = 0;
  let total = 0;
  for (let i = 0; i <= k; i += 1) {
    if (i > 0) logChoose += Math.log(n - i + 1) - Math.log(i);
    total += Math.exp(logChoose + i * logP + (n - i) * logQ);
  }
  return Math.min(total, 1);
};

/**
 * One-sided Clopper-Pearson upper bound after `failures` in `trials`.
 *
 * Null for zero trials, since nothing was measured. Bisection on the CDF,
 * which falls monotonically in `p`.
 */
export const clopperPearsonUpper = (
  failures: number,
  trials: number,
  confidence = 0.95,
): number | null => {
  if (
    !Number.isInteger(failures) ||
    !Number.isInteger(trials) ||
    failures < 0 ||
    failures > trials
  ) {
    throw new RangeError(
      `need 0 <= failures <= trials, got ${failures} of ${trials}`,
    );
  }
  if (trials === 0) return null;
  const alpha = 1 - confidence;
  let low = 0;
  let high = 1;
  for (let step = 0; step < 100; step += 1) {
    const middle = (low + high) / 2;
    if (binomialCdf(failures, trials, middle) > alpha) low = middle;
    else high = middle;
  }
  return high;
};

/**
 * The sentence the page shows for one snapshot's over-blocking.
 *
 * A record from before family counts existed says so, since its sibling
 * totals cannot say how many independent families they covered.
 */
export const describeOverBlocking = (overBlocking: OverBlocking): string => {
  const { familiesFailed, independentFamilies, upperBound95 } = overBlocking;
  if (familiesFailed === null || independentFamilies === null) {
    return `${overBlocking.siblingsFailed}/${overBlocking.siblingsRun} siblings failed, families not recorded`;
  }
  if (upperBound95 === null) {
    return "nothing measured, no sibling family completed";
  }
  return `${familiesFailed} of ${independentFamilies} families failed, true rate could be up to ${(upperBound95 * 100).toFixed(1)}%`;
};
