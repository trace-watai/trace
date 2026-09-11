import { ChevronLeft, ChevronRight } from "lucide-react";
import Link from "next/link";

import { TraceStepCard } from "@/components/trace/trace-step-card";
import type { TraceStep } from "@/lib/trace-steps";
import { cn } from "@/lib/utils";

interface TraceStepperProps {
  steps: TraceStep[];
  currentStepId: number;
  runId: string;
}

/**
 * One step visible at a time, with a next arrow (right) and once past the
 * first step. A previous arrow (left). Plain links to `?step=`, so paging
 * works without client JS and each step stays a shareable URL.
 */
export const TraceStepper = ({
  steps,
  currentStepId,
  runId,
}: TraceStepperProps) => {
  if (steps.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        This run has no recorded decision steps.
      </p>
    );
  }

  const index = steps.findIndex((step) => step.stepId === currentStepId);
  const currentIndex = index === -1 ? 0 : index;
  const current = steps[currentIndex];
  const previous = currentIndex > 0 ? steps[currentIndex - 1] : null;
  const next = currentIndex < steps.length - 1 ? steps[currentIndex + 1] : null;

  return (
    <div className="space-y-4">
      <p className="text-center text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        Step {currentIndex + 1} of {steps.length}
      </p>

      <div className="grid grid-cols-[auto_1fr_auto] items-center gap-4">
        <StepNavLink runId={runId} step={previous} direction="previous" />
        <TraceStepCard step={current} />
        <StepNavLink runId={runId} step={next} direction="next" />
      </div>
    </div>
  );
};

interface StepNavLinkProps {
  runId: string;
  step: TraceStep | null;
  direction: "previous" | "next";
}

/** Renders as a disabled placeholder (same size, no-op) when there's no such step. */
const StepNavLink = ({ runId, step, direction }: StepNavLinkProps) => {
  const isNext = direction === "next";
  const label = isNext ? "Next step" : "Previous step";
  const baseClassName =
    "flex w-10 shrink-0 items-center justify-center gap-1 rounded-md border px-2 py-2 text-sm font-medium transition-colors sm:w-28 sm:px-3";
  const content = isNext ? (
    <>
      <span className="hidden sm:inline">{label}</span>
      <ChevronRight aria-hidden className="h-4 w-4 shrink-0" />
    </>
  ) : (
    <>
      <ChevronLeft aria-hidden className="h-4 w-4 shrink-0" />
      <span className="hidden sm:inline">{label}</span>
    </>
  );

  if (!step) {
    return (
      <span
        aria-disabled="true"
        aria-label={label}
        className={cn(
          baseClassName,
          "cursor-not-allowed border-border/40 text-muted-foreground/40",
        )}
      >
        {content}
      </span>
    );
  }

  return (
    <Link
      href={`/runs/${runId}/trace?step=${step.stepId}`}
      aria-label={label}
      className={cn(
        baseClassName,
        "border-border/70 text-foreground hover:border-primary/40",
      )}
    >
      {content}
    </Link>
  );
};
