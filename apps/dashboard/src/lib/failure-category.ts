/**
 * Presentation logic for the {@link FailureCategory} vocabulary: turning the
 * snake_case wire values into human-facing labels. Kept separate from the type
 * so failure cards and attribution render categories identically.
 */

import type { FailureCategory } from "@/types/attribution";

export const failureCategoryLabel = (category: FailureCategory): string => {
  const spaced = category.replace(/_/g, " ");
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
};
