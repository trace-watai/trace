import type { ReactNode } from "react";

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { BlastRadius } from "@/types/failure-card";

interface BlastRadiusSummaryProps {
  blastRadius: BlastRadius;
}

const usd = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
});

/** At or above this many affected customers, collapse the names behind a click. */
const CUSTOMER_LIST_COLLAPSE_THRESHOLD = 5;

/**
 * Structured scope of external impact (0.4.0): money moved, durable records
 * created, customers affected — rendered as discrete stats rather than the
 * plain-text `summary` fallback, which stays for non-visual contexts.
 */
export const BlastRadiusSummary = ({
  blastRadius,
}: BlastRadiusSummaryProps) => {
  const {
    refundCount,
    refundTotalUsd,
    ticketCount,
    escalationCount,
    customersAffected,
  } = blastRadius;

  return (
    <div className="space-y-3">
      <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat label="Refunded">
          {usd.format(refundTotalUsd)}
          {refundCount > 0 && (
            <span className="ml-1 text-sm font-normal text-muted-foreground">
              · {refundCount} refund{refundCount === 1 ? "" : "s"}
            </span>
          )}
        </Stat>
        <Stat label="Ticket records">{ticketCount}</Stat>
        <Stat label="Escalations">{escalationCount}</Stat>
        <Stat label="Customers">{customersAffected.length}</Stat>
      </dl>

      {customersAffected.length > 0 &&
        (customersAffected.length >= CUSTOMER_LIST_COLLAPSE_THRESHOLD ? (
          <details className="group">
            <summary className="flex cursor-pointer list-none items-center gap-1 text-xs text-muted-foreground transition-colors hover:text-foreground">
              <span
                aria-hidden
                className="transition-transform group-open:rotate-90"
              >
                ▸
              </span>
              {customersAffected.length} customers affected
            </summary>
            <CustomerBadges customers={customersAffected} className="mt-2" />
          </details>
        ) : (
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-xs text-muted-foreground">
              Affected customers:
            </span>
            <CustomerBadges customers={customersAffected} inline />
          </div>
        ))}
    </div>
  );
};

interface CustomerBadgesProps {
  customers: string[];
  className?: string;
  inline?: boolean;
}

/** The affected-customer name badges, shared by the inline and collapsed views. */
const CustomerBadges = ({
  customers,
  className,
  inline,
}: CustomerBadgesProps) => (
  <div
    className={cn(
      !inline && "flex flex-wrap gap-1.5",
      inline && "contents",
      className,
    )}
  >
    {customers.map((customer) => (
      <Badge key={customer} variant="outline">
        {customer}
      </Badge>
    ))}
  </div>
);

interface StatProps {
  label: string;
  children: ReactNode;
}

const Stat = ({ label, children }: StatProps) => (
  <div className="rounded-md border border-primary/15 bg-primary/[0.03] px-3 py-2">
    <dt className="text-xs uppercase tracking-wide text-muted-foreground">
      {label}
    </dt>
    <dd className="font-mono text-xl font-semibold tabular-nums text-primary">
      {children}
    </dd>
  </div>
);
