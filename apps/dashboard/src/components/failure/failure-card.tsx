import type { ReactNode } from "react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { BlastRadiusSummary } from "@/components/failure/blast-radius-summary";
import { CategoryBadge } from "@/components/failure/category-badge";
import { SeverityBadge } from "@/components/failure/severity-badge";
import { LabeledBadge } from "@/components/ui/labeled-badge";
import { failureCategoryLabel } from "@/lib/failure-category";
import { briefFailureTitle } from "@/lib/failure-title";
import { cn } from "@/lib/utils";
import type { FailureCard as FailureCardData } from "@/types/failure-card";

interface FailureCardProps {
  card: FailureCardData;
}

/**
 * The human story of one verified failure, ordered by what to read first:
 *
 *   1. severity + primary category + title: what broke, how bad
 *   2. summary: the one-line story
 *   3. impact: the single figure that quantifies the damage
 *   4. symptoms, then contributing categories / steps as quiet supporting data
 *
 * Deeper drill-downs (root cause, evidence) belong to later views
 */
export const FailureCard = ({ card }: FailureCardProps) => {
  const [primaryCategory, ...otherCategories] = card.contributingFailures;

  return (
    <Card
      className={cn(
        "relative overflow-hidden border-primary/15",
        "shadow-[0_0_60px_-24px_hsl(var(--primary)/0.4)]",
        "before:absolute before:inset-x-0 before:top-0 before:h-px",
        "before:bg-gradient-to-r before:from-transparent before:via-primary/60 before:to-transparent",
      )}
    >
      <CardHeader className="gap-4">
        <div className="flex flex-wrap items-center gap-2">
          <SeverityBadge severity={card.severity} />
          {primaryCategory && (
            <LabeledBadge
              label="primary cause"
              value={failureCategoryLabel(primaryCategory)}
              valueClassName="bg-primary text-primary-foreground"
            />
          )}
        </div>

        <CardTitle className="text-balance text-xl font-semibold leading-snug">
          {briefFailureTitle(card.title)}
        </CardTitle>
      </CardHeader>

      <CardContent className="space-y-6">
        {card.causalExplanation && (
          <div className="space-y-2 rounded-lg border border-border/60 bg-muted/20 p-4">
            <FieldLabel>Quick overview</FieldLabel>
            <p className="text-sm leading-relaxed text-muted-foreground">
              {card.causalExplanation}
            </p>
          </div>
        )}

        <div className="space-y-2">
          <FieldLabel>Impact</FieldLabel>
          <BlastRadiusSummary blastRadius={card.blastRadius} />
        </div>

        {card.visibleSymptoms.length > 0 && (
          <div className="space-y-2">
            <FieldLabel>Visible symptoms</FieldLabel>
            <ul className="list-disc space-y-1 pl-5 text-sm leading-relaxed text-muted-foreground marker:text-primary/50">
              {card.visibleSymptoms.map((symptom, index) => (
                <li key={index}>{symptom}</li>
              ))}
            </ul>
          </div>
        )}

        {otherCategories.length > 0 && (
          <div className="space-y-2 border-t border-border/60 pt-4">
            <FieldLabel>Also contributed</FieldLabel>
            <div className="flex flex-wrap gap-1.5">
              {otherCategories.map((category) => (
                <CategoryBadge key={category} category={category} />
              ))}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
};

/**
 * Section label for a supporting field
 */
const FieldLabel = ({ children }: { children: ReactNode }) => (
  <h4 className="flex items-center gap-2 text-[0.7rem] font-bold uppercase tracking-[0.14em] text-white">
    <span aria-hidden className="h-3 w-0.5 rounded-full bg-primary/60" />
    {children}
  </h4>
);
