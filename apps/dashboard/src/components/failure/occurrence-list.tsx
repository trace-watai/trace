import Link from "next/link";

import { Badge } from "@/components/ui/badge";
import { occurrenceRows } from "@/lib/occurrences";
import { cn } from "@/lib/utils";
import type { BundleOccurrence } from "@/types/failure-card";

interface OccurrenceListProps {
  occurrences: BundleOccurrence[];
  /** The run whose page is showing the card, marked in the list. */
  currentRunId?: string;
}

/**
 * Every run a failure card covers (#211), in the order they were bundled. The
 * first is the run the card and its regression were pinned to, and the rest
 * reproduced the same bundle key. Renders nothing for a card written before
 * 0.5.0, which covers only its own run.
 */
export const OccurrenceList = ({
  occurrences,
  currentRunId,
}: OccurrenceListProps) => {
  if (occurrences.length === 0) return null;

  return (
    <ol className="space-y-1.5" aria-label="Occurrences">
      {occurrenceRows(occurrences, currentRunId).map((row) => (
        <li
          key={row.runId}
          className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm"
        >
          <Link
            href={`/runs/${row.runId}`}
            className={cn(
              "font-mono text-xs hover:underline",
              row.isCurrent ? "text-foreground" : "text-primary",
            )}
          >
            {row.runId}
          </Link>
          <Badge
            variant="outline"
            className="border-primary/25 font-normal text-muted-foreground"
          >
            {row.role === "first" ? "first, pinned" : "reproduction"}
          </Badge>
          {row.isCurrent && <Badge variant="secondary">this run</Badge>}
          <span className="text-xs text-muted-foreground">
            {row.taskId}
            {row.configuration && ` · ${row.configuration}`}
          </span>
        </li>
      ))}
    </ol>
  );
};
