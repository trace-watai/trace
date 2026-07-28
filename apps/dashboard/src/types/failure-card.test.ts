import { describe, expect, it } from "vitest";

import { FAILURE_CATEGORIES } from "@/types/attribution";
import {
  FAILURE_CARD_SCHEMA_VERSION,
  parseFailureCard,
  type RawFailureCard,
} from "@/types/failure-card";
// A real `failure_card.json` produced by `trace-harness run-pipeline`. Parsing
// it here guards the wire contract: if the Python schema drifts from these TS
// types, this test breaks instead of the UI silently rendering wrong.
import sampleArtifact from "@/data/sample-failure-card.json";

const raw = sampleArtifact as unknown as RawFailureCard;

describe("parseFailureCard on a Python-produced artifact", () => {
  it("parses the current schema version", () => {
    expect(raw.schema_version).toBe(FAILURE_CARD_SCHEMA_VERSION);
    expect(FAILURE_CARD_SCHEMA_VERSION).toBe("0.4.0");
  });

  it("camelizes structured blast radius (0.4.0 object shape)", () => {
    const card = parseFailureCard(raw);

    // Snake_case wire keys must survive as camelCase domain keys, values intact.
    expect(card.blastRadius).toMatchObject({
      refundCount: raw.blast_radius.refund_count,
      refundTotalUsd: raw.blast_radius.refund_total_usd,
      ticketCount: raw.blast_radius.ticket_count,
      escalationCount: raw.blast_radius.escalation_count,
      customersAffected: raw.blast_radius.customers_affected,
      summary: raw.blast_radius.summary,
    });
    expect(typeof card.blastRadius.refundTotalUsd).toBe("number");
  });

  it("keeps contributing categories in the closed vocabulary", () => {
    const card = parseFailureCard(raw);

    expect(card.contributingFailures.length).toBeGreaterThan(0);
    for (const category of card.contributingFailures) {
      expect(FAILURE_CATEGORIES).toContain(category);
    }
  });

  it("preserves step ids for cross-linking", () => {
    const card = parseFailureCard(raw);

    expect(card.stepIds).toEqual(raw.step_ids);
    expect(card.stepIds.every((id) => Number.isInteger(id))).toBe(true);
  });
});
