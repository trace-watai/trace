import { ChevronRight } from "lucide-react";
import type { ReactNode } from "react";

import { SeverityBadge } from "@/components/failure/severity-badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { LabeledBadge } from "@/components/ui/labeled-badge";
import { BlocksReleaseBadge } from "@/components/verifier/blocks-release-badge";
import { EvidenceList } from "@/components/verifier/evidence-list";
import type { VerifierResult } from "@/types/verifier-result";

interface VerifierSummaryProps {
  runId: string;
  result: VerifierResult;
}

/** Run-level verdict: pass/fail, worst severity, release gate, warnings, and evidence not tied to one check. */
export const VerifierSummary = ({ runId, result }: VerifierSummaryProps) => (
  <Card className="border-primary/25 bg-primary/[0.03]">
    <CardHeader className="gap-3">
      <p className="text-[0.7rem] font-bold uppercase tracking-[0.14em] text-primary">
        Run verdict
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <LabeledBadge
          label="verdict"
          value={result.passed ? "Passed" : "Failed"}
          valueClassName={
            result.passed
              ? "bg-emerald-600 text-white"
              : "bg-red-600 text-white"
          }
        />
        {result.severity && <SeverityBadge severity={result.severity} />}
        <BlocksReleaseBadge blocksRelease={result.blocksRelease} />
      </div>
      <CardTitle className="text-base font-semibold">
        {result.passed
          ? "All checks passed"
          : `${result.failedChecks.length} ${
              result.failedChecks.length === 1 ? "check" : "checks"
            } failed`}
      </CardTitle>
      <p className="font-mono text-xs text-muted-foreground">
        {result.verifierId}
      </p>
    </CardHeader>

    {(result.warnings.length > 0 || result.evidence.length > 0) && (
      <CardContent className="space-y-4">
        {result.warnings.length > 0 && (
          <div className="space-y-2">
            <FieldLabel>Warnings</FieldLabel>
            <ul className="list-disc space-y-1 pl-5 text-sm leading-relaxed text-muted-foreground marker:text-primary/50">
              {result.warnings.map((warning, index) => (
                <li key={index}>{warning}</li>
              ))}
            </ul>
          </div>
        )}

        {result.evidence.length > 0 && (
          <details className="group border-t border-border/60 pt-3">
            <summary className="flex cursor-pointer list-none items-center gap-1 text-xs font-semibold uppercase tracking-wide text-primary transition-colors hover:text-primary/80">
              <ChevronRight
                aria-hidden
                strokeWidth={3}
                className="h-3.5 w-3.5 transition-transform group-open:rotate-90"
              />
              Run-level evidence ({result.evidence.length})
            </summary>
            <div className="mt-3">
              <EvidenceList runId={runId} evidence={result.evidence} />
            </div>
          </details>
        )}
      </CardContent>
    )}
  </Card>
);

const FieldLabel = ({ children }: { children: ReactNode }) => (
  <h4 className="text-[0.7rem] font-bold uppercase tracking-[0.14em] text-primary">
    {children}
  </h4>
);
