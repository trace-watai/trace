import fs from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

import {
  METRICS_SNAPSHOT_SCHEMA_VERSION,
  READABLE_METRICS_SNAPSHOT_VERSIONS,
  parseMetricsHistory,
  parseMetricsSnapshot,
  ratioValue,
  type RawMetricsSnapshot,
} from "@/types/metrics-snapshot";

const raw: RawMetricsSnapshot = {
  schema_version: METRICS_SNAPSHOT_SCHEMA_VERSION,
  commit: "fc9fbc781930f9dd932942f5209fb14e71fc4698",
  recorded_at: "2026-09-19T04:23:45.171723Z",
  coverage: {
    prescribed: 6,
    materializable: 1,
    validated: 2,
    accepted: 1,
    accepted_gating: 0,
    accepted_advisory: 1,
    accepted_over_prescribed: { numerator: 1, denominator: 6, value: 0.1667 },
    materializable_over_prescribed: {
      numerator: 1,
      denominator: 6,
      value: 0.1667,
    },
    unmapped_controls: [],
  },
  over_blocking: {
    siblings_run: 1,
    siblings_failed: 0,
    rate: { numerator: 0, denominator: 1, value: 0 },
    independent_families: 1,
    families_failed: 0,
    upper_bound_95: 0.95,
    sources: ["fixtures/controls/evidence/4f23/repair_validation.json"],
  },
  cost_of_learning: {
    validation_runs: 2,
    irreversible_actions: 1,
    money_moved_usd: 189,
    runs_not_retained: [],
  },
  suite_pass_rate: { numerator: 18, denominator: 29, value: 0.6207 },
  verified_failures: 11,
};

describe("parseMetricsSnapshot", () => {
  it("camelizes every field the page reads", () => {
    const snapshot = parseMetricsSnapshot(raw);
    expect(snapshot.coverage.acceptedOverPrescribed.numerator).toBe(1);
    expect(snapshot.overBlocking.siblingsRun).toBe(1);
    expect(snapshot.costOfLearning.moneyMovedUsd).toBe(189);
    expect(snapshot.suitePassRate.denominator).toBe(29);
    expect(snapshot.verifiedFailures).toBe(11);
  });

  it("reads the gating and advisory split", () => {
    const snapshot = parseMetricsSnapshot({
      ...raw,
      coverage: { ...raw.coverage, accepted_gating: 1, accepted_advisory: 0 },
    });
    expect(snapshot.coverage.acceptedGating).toBe(1);
    expect(snapshot.coverage.acceptedAdvisory).toBe(0);
  });

  it("reads a 0.1.0 record, which has no split, as all advisory", () => {
    const coverage = { ...raw.coverage };
    delete coverage.accepted_gating;
    delete coverage.accepted_advisory;
    const snapshot = parseMetricsSnapshot({
      ...raw,
      schema_version: "0.1.0",
      coverage,
    });
    expect(snapshot.coverage.acceptedGating).toBe(0);
    expect(snapshot.coverage.acceptedAdvisory).toBe(1);
  });

  it("derives the family bound from the counts and ignores the written one", () => {
    const lying = parseMetricsSnapshot({
      ...raw,
      over_blocking: { ...raw.over_blocking, upper_bound_95: 0.01 },
    });
    expect(lying.overBlocking.upperBound95).toBeCloseTo(0.95, 4);
  });

  it("reads a record from before family counts with no bound", () => {
    const overBlocking = { ...raw.over_blocking };
    delete overBlocking.independent_families;
    delete overBlocking.families_failed;
    delete overBlocking.upper_bound_95;
    const snapshot = parseMetricsSnapshot({
      ...raw,
      schema_version: "0.2.0",
      over_blocking: overBlocking,
    });
    expect(snapshot.overBlocking.independentFamilies).toBeNull();
    expect(snapshot.overBlocking.familiesFailed).toBeNull();
    expect(snapshot.overBlocking.upperBound95).toBeNull();
  });

  it("leaves artifact paths alone", () => {
    // camelizeKeys rewrites object keys and these are string array entries, so
    // a path keeps its underscores. Worth pinning because a rewritten path
    // would point the reader at a file that does not exist.
    const snapshot = parseMetricsSnapshot(raw);
    expect(snapshot.overBlocking.sources[0]).toBe(
      "fixtures/controls/evidence/4f23/repair_validation.json",
    );
  });
});

describe("ratioValue", () => {
  it("recomputes from the counts rather than trusting the serialized rate", () => {
    const lying = parseMetricsSnapshot({
      ...raw,
      suite_pass_rate: { numerator: 1, denominator: 2, value: 0.99 },
    });
    expect(ratioValue(lying.suitePassRate)).toBe(0.5);
  });

  it("reports null for an empty denominator so the chart draws a gap", () => {
    expect(ratioValue({ numerator: 0, denominator: 0 })).toBeNull();
  });
});

describe("parseMetricsHistory", () => {
  it("reads one snapshot per line, oldest first, ignoring blanks", () => {
    const contents = [
      JSON.stringify({ ...raw, commit: "aaa" }),
      "",
      JSON.stringify({ ...raw, commit: "bbb" }),
      "",
    ].join("\n");
    expect(parseMetricsHistory(contents).map((s) => s.commit)).toEqual([
      "aaa",
      "bbb",
    ]);
  });

  it("reads the committed history file", () => {
    const file = path.resolve(
      process.cwd(),
      "..",
      "..",
      "docs",
      "acceptance",
      "metrics_history.jsonl",
    );
    const history = parseMetricsHistory(fs.readFileSync(file, "utf8"));
    expect(history.length).toBeGreaterThan(0);
    for (const snapshot of history) {
      expect(READABLE_METRICS_SNAPSHOT_VERSIONS).toContain(
        snapshot.schemaVersion,
      );
      expect(snapshot.commit.length).toBeGreaterThan(0);
      expect(snapshot.coverage.acceptedOverPrescribed.denominator).toBe(
        snapshot.coverage.prescribed,
      );
      expect(
        snapshot.coverage.acceptedGating + snapshot.coverage.acceptedAdvisory,
      ).toBe(snapshot.coverage.accepted);
      if (snapshot.overBlocking.independentFamilies === null) {
        expect(snapshot.overBlocking.upperBound95).toBeNull();
      }
    }
  });
});
