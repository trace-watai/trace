/**
 * Presentation logic for the shared {@link Severity} scale: how a severity
 * reads (label) and looks (badge color). Kept separate from the type.
 */

import type { Severity } from "@/types/severity";

/** Human-facing label for a severity (e.g. `"critical"` -> `"Critical"`). */
export const severityLabel = (severity: Severity): string =>
  severity.charAt(0).toUpperCase() + severity.slice(1);

/**
 * Solid fill classes for the value half of a coverage-style severity badge,
 * escalating green (least bad) -> red (worst). Yellow takes dark text for
 * contrast; the darker fills take white.
 */
export const severityBadgeClasses = (severity: Severity): string => {
  const styles: Record<Severity, string> = {
    low: "bg-emerald-600 text-white",
    medium: "bg-yellow-400 text-black",
    high: "bg-orange-500 text-black",
    critical: "bg-red-600 text-white",
  };
  return styles[severity];
};
