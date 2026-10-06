import { ChevronRight } from "lucide-react";

import { StepIdList } from "@/components/failure/step-id-list";
import { evidenceKindLabel } from "@/lib/evidence-kind";
import type { EvidenceItem } from "@/types/evidence";

interface EvidenceListProps {
  runId: string;
  evidence: EvidenceItem[];
}

/**
 * Evidence backing a check or the run as a whole: kind, description, the
 * steps it came from (linked to the stepper), and the raw data behind the
 * same native `<details>` collapse used elsewhere in the dashboard.
 */
export const EvidenceList = ({ runId, evidence }: EvidenceListProps) => {
  if (evidence.length === 0) return null;

  return (
    <ul className="space-y-3">
      {evidence.map((item, index) => (
        <li
          key={index}
          className="space-y-1.5 rounded-md border border-border/60 p-3"
        >
          <p className="text-[0.7rem] font-semibold uppercase tracking-wide text-primary">
            {evidenceKindLabel(item.kind)}
          </p>
          <p className="text-sm leading-relaxed text-muted-foreground">
            {item.description}
          </p>
          <StepIdList runId={runId} stepIds={item.stepIds} />
          {Object.keys(item.data).length > 0 && (
            <details className="group">
              <summary className="flex cursor-pointer list-none items-center gap-1 text-xs font-semibold uppercase tracking-wide text-primary transition-colors hover:text-primary/80">
                <ChevronRight
                  aria-hidden
                  strokeWidth={3}
                  className="h-3.5 w-3.5 transition-transform group-open:rotate-90"
                />
                Data
              </summary>
              <pre className="mt-2 whitespace-pre-wrap break-words rounded-md bg-muted/40 p-2 font-mono text-xs text-foreground">
                {JSON.stringify(item.data, null, 2)}
              </pre>
            </details>
          )}
        </li>
      ))}
    </ul>
  );
};
