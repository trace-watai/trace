import { ChevronRight } from "lucide-react";
import type { ReactNode } from "react";

import { SeverityBadge } from "@/components/failure/severity-badge";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { LabeledBadge } from "@/components/ui/labeled-badge";
import { docStatusBadgeClasses, docStatusLabel } from "@/lib/doc-status";
import { stepMarkerLabel, stepTitle, type TraceStep } from "@/lib/trace-steps";
import { cn } from "@/lib/utils";

interface TraceStepCardProps {
  step: TraceStep;
}

/**
 * One agent decision step in a consistent format: step number + markers,
 * title, details (reasoning/action/observation), then evidence (retrieved
 * docs, failed checks) collapsed behind a native `<details>`, same
 * no-client-JS collapse pattern as `blast-radius-summary.tsx`.
 */
export const TraceStepCard = ({ step }: TraceStepCardProps) => {
  const hasFailedChecks = step.failedChecks.length > 0;
  const hasRunError = step.runError !== null;
  const evidenceCount = step.retrievalResults.length + step.failedChecks.length;

  return (
    <Card
      id={`step-${step.stepId}`}
      className={cn(
        "flex h-[70vh] min-h-[24rem] min-w-0 scroll-mt-6 flex-col",
        (hasFailedChecks || hasRunError) &&
          "border-destructive/40 bg-destructive/[0.03]",
      )}
    >
      <CardHeader className="shrink-0 gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-mono text-xs font-semibold uppercase tracking-wide text-primary">
            Step {step.stepId}
          </span>
          {step.markers.map((marker) => (
            <Badge
              key={marker.kind}
              variant="outline"
              className="border-primary/40 bg-primary/10 font-semibold text-primary"
            >
              {stepMarkerLabel(marker.kind)}
            </Badge>
          ))}
        </div>
        <CardTitle className="text-base font-semibold">
          {stepTitle(step)}
        </CardTitle>
      </CardHeader>

      <CardContent className="min-h-0 flex-1 space-y-4 overflow-y-auto">
        {step.runError && (
          <div className="space-y-1 rounded-md border border-destructive/40 bg-destructive/10 p-3">
            <FieldLabel>Run error ({step.runError.kind})</FieldLabel>
            <p className="text-sm text-destructive">{step.runError.message}</p>
          </div>
        )}

        {step.reasoning && (
          <div className="space-y-1">
            <FieldLabel>Reasoning</FieldLabel>
            <p className="text-sm leading-relaxed text-muted-foreground">
              {step.reasoning}
            </p>
          </div>
        )}

        {step.action.kind === "tool_call" && (
          <div className="space-y-1">
            <FieldLabel>Action</FieldLabel>
            <p className="break-words font-mono text-sm text-foreground">
              {step.action.toolName}({JSON.stringify(step.action.arguments)})
            </p>
          </div>
        )}

        {step.action.kind === "final_answer" && (
          <div className="space-y-1">
            <FieldLabel>Final answer</FieldLabel>
            <p className="text-sm text-foreground">{step.action.finalAnswer}</p>
          </div>
        )}

        {step.observation && (
          <div className="space-y-1">
            <FieldLabel>Observation</FieldLabel>
            {step.observation.error ? (
              <p className="text-sm text-destructive">
                {step.observation.error}
              </p>
            ) : (
              <pre className="whitespace-pre-wrap break-words rounded-md bg-muted/40 p-2 font-mono text-xs text-foreground">
                {JSON.stringify(step.observation.result, null, 2)}
              </pre>
            )}
          </div>
        )}

        {evidenceCount > 0 && (
          <details className="group border-t border-border/60 pt-3">
            <summary className="flex cursor-pointer list-none items-center gap-1 text-xs font-semibold uppercase tracking-wide text-primary transition-colors hover:text-primary/80">
              <ChevronRight
                aria-hidden
                strokeWidth={3}
                className="h-3.5 w-3.5 transition-transform group-open:rotate-90"
              />
              Evidence ({evidenceCount})
            </summary>
            <div className="mt-3 space-y-3">
              {step.retrievalResults.length > 0 && (
                <div className="space-y-1.5">
                  <FieldLabel>Retrieved docs</FieldLabel>
                  <div className="flex flex-wrap gap-1.5">
                    {step.retrievalResults.map((doc) => (
                      <LabeledBadge
                        key={doc.docId}
                        label={doc.docId}
                        value={docStatusLabel(doc.status)}
                        valueClassName={docStatusBadgeClasses(doc.status)}
                      />
                    ))}
                  </div>
                </div>
              )}

              {step.failedChecks.map((check) => (
                <div
                  key={check.checkId}
                  className="space-y-1 rounded-md border border-border/60 p-3"
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <SeverityBadge severity={check.severity} />
                    <span className="font-mono text-xs text-muted-foreground">
                      {check.checkId}
                    </span>
                  </div>
                  <p className="text-sm text-muted-foreground">
                    {check.message}
                  </p>
                  <p className="text-xs text-muted-foreground">
                    Expected: {check.expected} — Actual: {check.actual}
                  </p>
                </div>
              ))}
            </div>
          </details>
        )}
      </CardContent>
    </Card>
  );
};

const FieldLabel = ({ children }: { children: ReactNode }) => (
  <h4 className="text-[0.7rem] font-bold uppercase tracking-[0.14em] text-primary">
    {children}
  </h4>
);
