import type { ComponentProps, ReactNode } from "react";

import { cn } from "@/lib/utils";

interface LabeledBadgeProps extends Omit<ComponentProps<"span">, "children"> {
  /** Neutral left segment naming the field (e.g. "severity", "cause"). */
  label: string;
  /** Right segment carrying the value. */
  value: ReactNode;
  /** Fill/text classes for the value segment (its color carries meaning). */
  valueClassName?: string;
}

/**
 * Coverage-style two-segment badge: a neutral label joined to a colored value
 * segment. The shared shape behind the severity and primary-cause badges so status 
 * chips read consistently.
 */
export const LabeledBadge = ({
  label,
  value,
  valueClassName,
  className,
  ...props
}: LabeledBadgeProps) => (
  <span
    className={cn(
      "inline-flex select-none items-stretch overflow-hidden rounded text-[0.7rem] font-medium leading-none tracking-wide",
      className,
    )}
    {...props}
  >
    <span className="flex items-center bg-secondary px-2 py-1 uppercase text-secondary-foreground">
      {label}
    </span>
    <span
      className={cn(
        "flex items-center px-2 py-1 font-semibold uppercase",
        valueClassName,
      )}
    >
      {value}
    </span>
  </span>
);
