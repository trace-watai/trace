import { readFileSync } from "node:fs";
import path from "node:path";
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
import sampleArtifact from "@/fixtures/refund-failure/failure_card.json";

const raw = sampleArtifact as unknown as RawFailureCard;
const REPO = path.join(process.cwd(), "..", "..");

describe("parseFailureCard on a Python-produced artifact", () => {
  it("parses the current schema version", () => {
    expect(raw.schema_version).toBe(FAILURE_CARD_SCHEMA_VERSION);
    expect(FAILURE_CARD_SCHEMA_VERSION).toBe("0.5.0");
  });

  it("mirrors the backend's schema version", () => {
    const backend = readFileSync(
      path.join(REPO, "src", "trace_harness", "failure_bundles", "schemas.py"),
      "utf8",
    ).match(/^FAILURE_CARD_SCHEMA_VERSION = "([^"]+)"$/m)?.[1];

    expect(FAILURE_CARD_SCHEMA_VERSION).toBe(backend);
  });

  it("carries its bundle key and lists its own run as the first occurrence", () => {
    const card = parseFailureCard(raw);

    expect(card.bundleKey).toBe(raw.bundle_key);
    expect(card.bundleKey).toMatch(/^v1:stale_source_authority:issue_refund:/);
    expect(card.occurrences).toEqual([
      {
        runId: raw.run_id,
        taskId: raw.task_id,
        provider: "fixture",
        model: "scripted:refund_policy_failure_script",
        seed: null,
      },
    ]);
  });

  it("reads a card written before 0.5.0 as covering its own run alone", () => {
    const older: RawFailureCard = { ...raw, schema_version: "0.4.0" };
    delete older.bundle_key;
    delete older.occurrences;

    const card = parseFailureCard(older);

    expect(card.bundleKey).toBeNull();
    expect(card.occurrences).toEqual([]);
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
