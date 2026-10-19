import { readFileSync } from "node:fs";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { FailureCard } from "@/components/failure/failure-card";
import { TraceStepper } from "@/components/trace/trace-stepper";
import { buildTraceSteps } from "@/lib/trace-steps";
import { parseAttributionResult } from "@/types/attribution";
import { parseFailureCard } from "@/types/failure-card";
import {
  TRACE_SCHEMA_VERSION,
  parseTrace,
  type RawTraceEvent,
} from "@/types/trace-event";
import { parseVerifierResult } from "@/types/verifier-result";

// A fixture-only run produced by the Python pipeline, not a second hand-made
// copy of its contracts. No new workflow-specific renderer is needed.
const sample = JSON.parse(
  readFileSync(
    new URL(
      "../../../../../fixtures/expected/retrieval_stale_document_artifacts.json",
      import.meta.url,
    ),
    "utf8",
  ),
);

describe("retrieval workflow through existing dashboard views", () => {
  it("preserves section ids and spans in the typed retrieval payload", () => {
    const trace = parseTrace(sample.trace as RawTraceEvent[]);
    expect(new Set(trace.map((event) => event.schemaVersion))).toEqual(
      new Set([TRACE_SCHEMA_VERSION]),
    );
    const retrieval = trace.find(
      (event) => event.eventType === "retrieval_result",
    );
    expect(retrieval).toBeDefined();
    if (retrieval?.eventType !== "retrieval_result")
      throw new Error("missing retrieval");
    expect(retrieval.payload.results[0]).toMatchObject({
      sectionId: "retention",
      spanStart: 0,
    });
    expect(retrieval.payload.results[0].spanEnd).toBeGreaterThan(0);
  });

  it("renders retrieval evidence, final-answer failures and the failure card", () => {
    const trace = parseTrace(sample.trace as RawTraceEvent[]);
    const attribution = parseAttributionResult(sample.attribution_result);
    const verifier = parseVerifierResult(sample.verifier_result);
    const card = parseFailureCard(sample.failure_card);
    const steps = buildTraceSteps(trace, attribution, verifier);
    expect(steps).toHaveLength(3);
    const render = (stepId: number) =>
      renderToStaticMarkup(
        <TraceStepper
          steps={steps}
          currentStepId={stepId}
          runId={card.runId}
        />,
      );
    expect(render(1)).toContain("retention_policy_v2");
    expect(render(2)).toContain("spanStart");
    expect(render(3)).toContain("retrieval_stale_document");
    expect(render(3)).toContain("Visible symptom");
    const markup = renderToStaticMarkup(<FailureCard card={card} />);
    expect(markup).toContain("non-current documents");
  });
});
