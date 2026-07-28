import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

interface StepIdListProps {
  stepIds: number[];
  className?: string;
}

/**
 * Row of step-id chips. Step ids are the cross-linking currency across the
 * dashboard (every evidence item carries them), so this stays a shared
 * primitive rather than living inside one view.
 */
export const StepIdList = ({ stepIds, className }: StepIdListProps) => {
  if (stepIds.length === 0) return null;

  return (
    <div className={cn("flex flex-wrap gap-1.5", className)}>
      {stepIds.map((stepId) => (
        <Badge
          key={stepId}
          variant="outline"
          className="border-primary/25 bg-primary/[0.04] font-mono text-primary/90"
        >
          Step {stepId}
        </Badge>
      ))}
    </div>
  );
};
