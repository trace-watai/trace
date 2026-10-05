import { ChevronLeft, ChevronRight } from "lucide-react";
import Link from "next/link";

import { cn } from "@/lib/utils";

interface FailedCheckPagerProps {
  runId: string;
  /** 1-based position of the check being shown. */
  position: number;
  total: number;
  previousCheckId: string | null;
  nextCheckId: string | null;
}

/**
 * Previous / next controls for stepping through failed checks one at a time.
 * Plain links to `?check=`, like the trace stepper, so paging works without
 * client JS and each check stays a shareable URL.
 */
export const FailedCheckPager = ({
  runId,
  position,
  total,
  previousCheckId,
  nextCheckId,
}: FailedCheckPagerProps) => (
  <div className="flex items-center justify-between gap-4">
    <PagerLink runId={runId} checkId={previousCheckId} direction="previous" />
    <p className="text-center text-xs font-semibold uppercase tracking-wide text-muted-foreground">
      Check {position} of {total}
    </p>
    <PagerLink runId={runId} checkId={nextCheckId} direction="next" />
  </div>
);

interface PagerLinkProps {
  runId: string;
  checkId: string | null;
  direction: "previous" | "next";
}

/** Disabled placeholder (same size, no-op) when there's no such check. */
const PagerLink = ({ runId, checkId, direction }: PagerLinkProps) => {
  const isNext = direction === "next";
  const label = isNext ? "Next" : "Previous";
  const className =
    "flex w-28 shrink-0 items-center justify-center gap-1 rounded-md border px-3 py-2 text-sm font-medium transition-colors";
  const content = isNext ? (
    <>
      <span>{label}</span>
      <ChevronRight aria-hidden className="h-4 w-4 shrink-0" />
    </>
  ) : (
    <>
      <ChevronLeft aria-hidden className="h-4 w-4 shrink-0" />
      <span>{label}</span>
    </>
  );

  if (!checkId) {
    return (
      <span
        aria-disabled="true"
        className={cn(
          className,
          "cursor-not-allowed border-border/40 text-muted-foreground/40",
        )}
      >
        {content}
      </span>
    );
  }

  return (
    <Link
      href={`/runs/${runId}/verifier?check=${encodeURIComponent(checkId)}`}
      className={cn(
        className,
        "border-border/70 text-foreground hover:border-primary/40",
      )}
    >
      {content}
    </Link>
  );
};
