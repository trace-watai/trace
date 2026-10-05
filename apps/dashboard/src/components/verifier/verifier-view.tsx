import { FailedCheckCard } from "@/components/verifier/failed-check-card";
import { FailedCheckPager } from "@/components/verifier/failed-check-pager";
import { VerifierSummary } from "@/components/verifier/verifier-summary";
import { resolveCheckIndex, sortedFailedChecks } from "@/lib/verifier-checks";
import type { VerifierResult } from "@/types/verifier-result";

interface VerifierViewProps {
  runId: string;
  result: VerifierResult;
  /** Requested `?check=` id; the first (most severe) check when absent or unknown. */
  currentCheckId?: string;
}

/**
 * The run-level verdict, then the failed checks one at a time, most severe
 * first, paged with previous / next.
 */
export const VerifierView = ({
  runId,
  result,
  currentCheckId,
}: VerifierViewProps) => {
  const checks = sortedFailedChecks(result.failedChecks);
  const index = resolveCheckIndex(checks, currentCheckId);
  const current = checks[index];

  return (
    <div className="space-y-8">
      <VerifierSummary runId={runId} result={result} />

      <section aria-labelledby="failed-checks-heading" className="space-y-4">
        <h2
          id="failed-checks-heading"
          className="flex items-center gap-2 text-sm font-semibold tracking-tight"
        >
          Failed checks
          <span className="rounded-full bg-muted px-2 py-0.5 font-mono text-xs text-muted-foreground">
            {checks.length}
          </span>
        </h2>

        {current ? (
          <>
            <FailedCheckPager
              runId={runId}
              position={index + 1}
              total={checks.length}
              previousCheckId={checks[index - 1]?.checkId ?? null}
              nextCheckId={checks[index + 1]?.checkId ?? null}
            />
            <FailedCheckCard runId={runId} check={current} />
          </>
        ) : (
          <p className="text-sm text-muted-foreground">
            No failed checks — every check the verifier ran passed.
          </p>
        )}
      </section>
    </div>
  );
};
