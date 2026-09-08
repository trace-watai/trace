/**
 * Presentation logic for a retrieval result's document status: how it reads
 * (label) and looks (badge color). Mirrors the backend `DocStatus` StrEnum
 * (`src/trace_harness/environment/state.py`): `current` is authoritative,
 * `deprecated` exists but must not drive decisions, `resolved` marks closed
 * incidents that read as relevant but no longer apply.
 *
 * `retrieval_result.status` is a loose `string` on the wire (not every
 * scenario is guaranteed to emit one of the three known values), so this
 * degrades to a neutral style for anything else rather than throwing.
 */

const KNOWN_STATUS_STYLES: Record<string, string> = {
  current: "bg-emerald-600 text-white",
  deprecated: "bg-orange-500 text-black",
  resolved: "bg-slate-500 text-white",
};

const KNOWN_STATUS_LABELS: Record<string, string> = {
  current: "Current",
  deprecated: "Deprecated",
  resolved: "Resolved",
};

/** Human-facing label for a doc status; title-cases anything unrecognized. */
export const docStatusLabel = (status: string): string =>
  KNOWN_STATUS_LABELS[status] ??
  status.charAt(0).toUpperCase() + status.slice(1);

/** Solid fill classes for a doc-status badge; neutral gray for the unknown case. */
export const docStatusBadgeClasses = (status: string): string =>
  KNOWN_STATUS_STYLES[status] ?? "bg-muted text-muted-foreground";
