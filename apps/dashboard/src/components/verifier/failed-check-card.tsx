import { ChevronRight } from "lucide-react";
import type { ReactNode } from "react";

import { SeverityBadge } from "@/components/failure/severity-badge";
import { StepIdList } from "@/components/failure/step-id-list";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { BlocksReleaseBadge } from "@/components/verifier/blocks-release-badge";
import { EvidenceList } from "@/components/verifier/evidence-list";
import type { FailedCheck } from "@/types/verifier-result";

interface FailedCheckCardProps {
  runId: string;
  check: FailedCheck;
}

/** One failed deterministic check: what fired, expected vs actual, and where. */
export const FailedCheckCard = ({ runId, check }: FailedCheckCardProps) => (
  <Card>
    <CardHeader className="gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <SeverityBadge severity={check.severity} />
        <BlocksReleaseBadge blocksRelease={check.blocksRelease} />
      </div>
      <p className="break-words font-mono text-sm font-semibold text-foreground">
        {check.checkId}
      </p>
    </CardHeader>

    <CardContent className="space-y-4">
      <p className="text-sm leading-relaxed text-muted-foreground">
        {check.message}
      </p>

      <div className="grid gap-3 sm:grid-cols-2">
        <div className="space-y-1">
          <FieldLabel>Expected</FieldLabel>
          <p className="text-sm text-foreground">{check.expected}</p>
        </div>
        <div className="space-y-1">
          <FieldLabel>Actual</FieldLabel>
          <p className="text-sm text-foreground">{check.actual}</p>
        </div>
      </div>

      {check.stepIds.length > 0 && (
        <div className="space-y-1.5">
          <FieldLabel>Steps</FieldLabel>
          <StepIdList runId={runId} stepIds={check.stepIds} />
        </div>
      )}

      {check.evidence.length > 0 && (
        <details className="group border-t border-border/60 pt-3">
          <summary className="flex cursor-pointer list-none items-center gap-1 text-xs font-semibold uppercase tracking-wide text-primary transition-colors hover:text-primary/80">
            <ChevronRight
              aria-hidden
              strokeWidth={3}
              className="h-3.5 w-3.5 transition-transform group-open:rotate-90"
            />
            Evidence ({check.evidence.length})
          </summary>
          <div className="mt-3">
            <EvidenceList runId={runId} evidence={check.evidence} />
          </div>
        </details>
      )}
    </CardContent>
  </Card>
);

const FieldLabel = ({ children }: { children: ReactNode }) => (
  <h4 className="text-[0.7rem] font-bold uppercase tracking-[0.14em] text-primary">
    {children}
  </h4>
);
