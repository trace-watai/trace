/**
 * Shared severity scale.
 *
 * Mirrors `Severity` in `src/trace_harness/tasks/schemas.py` — used across
 * tasks, verifier checks, and failure cards.
 */

/**
 * Severity scale, ordered low → critical; index doubles as the rank for
 * sorting (mirrors the backend `_SEVERITY_ORDER`).
 */
export const SEVERITIES = ["low", "medium", "high", "critical"] as const;

export type Severity = (typeof SEVERITIES)[number];

/** Numeric rank for a severity, low (0) → critical (3). */
export const severityRank = (severity: Severity): number =>
  SEVERITIES.indexOf(severity);
