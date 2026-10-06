import { severityRank } from "@/types/severity";
import type { FailedCheck } from "@/types/verifier-result";

/** Index of the requested check id in `checks`, or 0 when absent or unknown. */
export const resolveCheckIndex = (
  checks: FailedCheck[],
  requestedCheckId: string | undefined,
): number => {
  const index = checks.findIndex((c) => c.checkId === requestedCheckId);
  return index === -1 ? 0 : index;
};

/**
 * Failed checks ordered most severe first. Checks of equal severity keep the
 * verifier's own order (the sort is stable), and the input is not mutated.
 */
export const sortedFailedChecks = (checks: FailedCheck[]): FailedCheck[] =>
  [...checks].sort(
    (a, b) => severityRank(b.severity) - severityRank(a.severity),
  );
