import type { BundleOccurrence } from "@/types/failure-card";

/** One line of a failure card's occurrence list (#211). */
export interface OccurrenceRow {
  runId: string;
  taskId: string;
  /** The first occurrence is the run the card, repair package and regression were pinned to. */
  role: "first" | "reproduction";
  /** The run whose page is showing the card. */
  isCurrent: boolean;
  /** Provider, model and seed where recorded, e.g. "fixture · scripted:x · seed 2". */
  configuration: string | null;
}

export const occurrenceRows = (
  occurrences: BundleOccurrence[],
  currentRunId?: string,
): OccurrenceRow[] =>
  occurrences.map((occurrence, index) => {
    const parts = [
      occurrence.provider,
      occurrence.model,
      occurrence.seed === null ? null : `seed ${occurrence.seed}`,
    ].filter((part): part is string => Boolean(part));
    return {
      runId: occurrence.runId,
      taskId: occurrence.taskId,
      role: index === 0 ? "first" : "reproduction",
      isCurrent: occurrence.runId === currentRunId,
      configuration: parts.length > 0 ? parts.join(" · ") : null,
    };
  });
