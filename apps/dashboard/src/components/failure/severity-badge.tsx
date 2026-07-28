import type { ComponentProps } from "react";

import { LabeledBadge } from "@/components/ui/labeled-badge";
import { severityBadgeClasses, severityLabel } from "@/lib/severity-style";
import type { Severity } from "@/types/severity";

interface SeverityBadgeProps extends Omit<
  ComponentProps<typeof LabeledBadge>,
  "label" | "value"
> {
  severity: Severity;
}

/**
 * Coverage-style status badge for a {@link Severity}: a neutral "severity"
 * label joined to a solid color segment that carries the meaning. green
 * (low) → yellow → orange → red (critical). Shared across failure cards,
 * verifier checks, and attribution so the scale reads the same everywhere.
 */
export const SeverityBadge = ({ severity, ...props }: SeverityBadgeProps) => (
  <LabeledBadge
    label="severity"
    value={severityLabel(severity)}
    valueClassName={severityBadgeClasses(severity)}
    {...props}
  />
);
