import type { ComponentProps } from "react";

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import { failureCategoryLabel } from "@/lib/failure-category";
import type { FailureCategory } from "@/types/attribution";

interface CategoryBadgeProps extends Omit<
  ComponentProps<typeof Badge>,
  "variant" | "children"
> {
  category: FailureCategory;
  /** Emphasize the primary category (the first / most important one). */
  primary?: boolean;
}

/**
 * Chip for one {@link FailureCategory}. Shared by failure cards and attribution
 * so the taxonomy reads consistently across views.
 */
export const CategoryBadge = ({
  category,
  primary = false,
  className,
  ...props
}: CategoryBadgeProps) => (
  <Badge
    variant="outline"
    className={cn(
      "border-border/70",
      primary && "border-primary/40 bg-primary/10 font-semibold text-primary",
      className,
    )}
    {...props}
  >
    {failureCategoryLabel(category)}
  </Badge>
);
