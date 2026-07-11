import { existsSync, readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import {
  FAILURE_CARD_SCHEMA_VERSION,
  parseFailureCard,
  type RawFailureCard,
} from "@/types/failure-card";
import { parseRunResult, type RawRunResult } from "@/types/run-result";

const fixtureUrl = (name: string): URL => new URL(name, import.meta.url);
const readFixture = <T>(name: string): T =>
  JSON.parse(readFileSync(fixtureUrl(name), "utf8")) as T;

describe("refund-failure fixture contracts", () => {
  it("matches the current FailureCard contract", () => {
    const card = parseFailureCard(
      readFixture<RawFailureCard>("failure_card.json"),
    );

    expect(card.schemaVersion).toBe(FAILURE_CARD_SCHEMA_VERSION);
    expect(card.contributingFailures.length).toBeGreaterThan(0);
    expect(card.stepIds).toEqual([3, 4, 5, 6]);
  });

  it("has artifact paths that resolve inside the flat fixture bundle", () => {
    const result = parseRunResult(readFixture<RawRunResult>("run_result.json"));

    for (const path of Object.values(result.artifactPaths)) {
      expect(existsSync(fixtureUrl(path)), path).toBe(true);
    }
  });
});
