import { existsSync, readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import {
  ATTRIBUTION_SCHEMA_VERSION,
  parseAttributionResult,
  type RawAttributionResult,
} from "@/types/attribution";
import {
  FAILURE_CARD_SCHEMA_VERSION,
  parseFailureCard,
  type RawFailureCard,
} from "@/types/failure-card";
import {
  REGRESSION_SCHEMA_VERSION,
  parseRegressionArtifact,
  type RawRegressionArtifact,
} from "@/types/regression-artifact";
import {
  REPAIR_PACKAGE_SCHEMA_VERSION,
  parseRepairPackage,
  type RawRepairPackage,
} from "@/types/repair-package";
import { parseRunResult, type RawRunResult } from "@/types/run-result";
import {
  TRACE_SCHEMA_VERSION,
  parseTrace,
  type RawTraceEvent,
} from "@/types/trace-event";
import {
  VERIFIER_RESULT_SCHEMA_VERSION,
  parseVerifierResult,
  type RawVerifierResult,
} from "@/types/verifier-result";

const fixtureUrl = (name: string): URL => new URL(name, import.meta.url);
const readFixture = <T>(name: string): T =>
  JSON.parse(readFileSync(fixtureUrl(name), "utf8")) as T;

describe("refund-failure fixture contracts", () => {
  it("represents the current escalation-aware task contract", () => {
    const task = readFixture<{
      schema_version: string;
      task_id: string;
      requires_escalation: boolean;
    }>("task_spec.json");

    expect(task.schema_version).toBe("0.3.0");
    expect(task.task_id).toBe("refund_policy_failure");
    expect(task.requires_escalation).toBe(true);
  });

  it("matches the current FailureCard contract", () => {
    const card = parseFailureCard(
      readFixture<RawFailureCard>("failure_card.json"),
    );

    expect(card.schemaVersion).toBe(FAILURE_CARD_SCHEMA_VERSION);
    expect(card.contributingFailures.length).toBeGreaterThan(0);
    expect(card.stepIds).toEqual([3, 4, 5, 6, 7]);
  });

  it("keeps every derived artifact on its current contract and source run", () => {
    const attribution = parseAttributionResult(
      readFixture<RawAttributionResult>("attribution_result.json"),
    );
    const verifier = parseVerifierResult(
      readFixture<RawVerifierResult>("verifier_result.json"),
    );
    const repair = parseRepairPackage(
      readFixture<RawRepairPackage>("repair_package.json"),
    );
    const regression = parseRegressionArtifact(
      readFixture<RawRegressionArtifact>("regression_artifact.json"),
    );
    const run = parseRunResult(readFixture<RawRunResult>("run_result.json"));

    expect(attribution.schemaVersion).toBe(ATTRIBUTION_SCHEMA_VERSION);
    expect(verifier.schemaVersion).toBe(VERIFIER_RESULT_SCHEMA_VERSION);
    expect(repair.schemaVersion).toBe(REPAIR_PACKAGE_SCHEMA_VERSION);
    expect(regression.schemaVersion).toBe(REGRESSION_SCHEMA_VERSION);
    expect(
      new Set([
        attribution.runId,
        verifier.runId,
        repair.runId,
        regression.sourceRunId,
        run.runId,
      ]),
    ).toEqual(new Set([run.runId]));
    expect(verifier.passed).toBe(false);
    expect(verifier.blocksRelease).toBe(true);
  });

  it("parses the complete trace on the current event contract", () => {
    const events = readFileSync(fixtureUrl("trace.jsonl"), "utf8")
      .trim()
      .split("\n")
      .map((line) => JSON.parse(line) as RawTraceEvent);
    const trace = parseTrace(events);

    expect(trace.length).toBeGreaterThan(0);
    expect(
      trace.every((event) => event.schemaVersion === TRACE_SCHEMA_VERSION),
    ).toBe(true);
    expect(trace.at(0)?.eventType).toBe("run_started");
    expect(trace.at(-1)?.eventType).toBe("run_finished");
  });

  it("has artifact paths that resolve inside the flat fixture bundle", () => {
    const result = parseRunResult(readFixture<RawRunResult>("run_result.json"));

    for (const path of Object.values(result.artifactPaths)) {
      expect(existsSync(fixtureUrl(path)), path).toBe(true);
    }
  });
});
