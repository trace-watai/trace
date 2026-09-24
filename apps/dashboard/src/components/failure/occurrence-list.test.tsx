import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { OccurrenceList } from "@/components/failure/occurrence-list";
import type { BundleOccurrence } from "@/types/failure-card";

const occurrence = (runId: string): BundleOccurrence => ({
  runId,
  taskId: "refund_policy_failure",
  provider: "fixture",
  model: "scripted:refund_policy_failure_script",
  seed: null,
});

const links = (markup: string): string[] => markup.match(/<a\b[^>]*>/g) ?? [];

describe("OccurrenceList", () => {
  it("marks only the link to the run being viewed as the current page", () => {
    const markup = renderToStaticMarkup(
      <OccurrenceList
        occurrences={[occurrence("run_a"), occurrence("run_b")]}
        currentRunId="run_b"
      />,
    );

    const [first, second] = links(markup);
    expect(first).not.toContain("aria-current");
    expect(second).toContain('aria-current="page"');
  });

  it("takes its name from the visible heading when given one", () => {
    const labelled = renderToStaticMarkup(
      <OccurrenceList
        occurrences={[occurrence("run_a")]}
        labelledBy="occurrences-heading"
      />,
    );
    const bare = renderToStaticMarkup(
      <OccurrenceList occurrences={[occurrence("run_a")]} />,
    );

    expect(labelled).toContain('aria-labelledby="occurrences-heading"');
    expect(labelled).not.toContain("aria-label=");
    expect(bare).toContain('aria-label="Occurrences"');
  });
});
