import { readFileSync } from "node:fs";
import { join } from "node:path";

import rawAttributionResult from "@/fixtures/refund-failure/attribution_result.json";
import rawFailureCard from "@/fixtures/refund-failure/failure_card.json";
import rawFinalState from "@/fixtures/refund-failure/final_state.json";
import rawInitialState from "@/fixtures/refund-failure/initial_state.json";
import rawRegressionArtifact from "@/fixtures/refund-failure/regression_artifact.json";
import rawRepairPackage from "@/fixtures/refund-failure/repair_package.json";
import rawRunConfig from "@/fixtures/refund-failure/run_config.json";
import rawRunResult from "@/fixtures/refund-failure/run_result.json";
import rawTaskSpec from "@/fixtures/refund-failure/task_spec.json";
import rawVerifierResult from "@/fixtures/refund-failure/verifier_result.json";
import {
  parseAttributionResult,
  type AttributionResult,
  type RawAttributionResult,
} from "@/types/attribution";
import {
  parseFailureCard,
  type FailureCard,
  type RawFailureCard,
} from "@/types/failure-card";
import {
  parseRegressionArtifact,
  type RawRegressionArtifact,
  type RegressionArtifact,
} from "@/types/regression-artifact";
import {
  parseRepairPackage,
  type RawRepairPackage,
  type RepairPackage,
} from "@/types/repair-package";
import {
  parseRunResult,
  type RawRunResult,
  type RunResult,
} from "@/types/run-result";
import {
  parseTrace,
  type RawTraceEvent,
  type TraceEvent,
} from "@/types/trace-event";
import {
  parseVerifierResult,
  type RawVerifierResult,
  type VerifierResult,
} from "@/types/verifier-result";

export const REFUND_FAILURE_ARTIFACT_NAMES = [
  "task_spec.json",
  "run_config.json",
  "initial_state.json",
  "final_state.json",
  "trace.jsonl",
  "run_result.json",
  "verifier_result.json",
  "attribution_result.json",
  "failure_card.json",
  "repair_package.json",
  "regression_artifact.json",
] as const;

type RawArtifactRecord = Record<string, unknown>;

export interface RefundFailureFixture {
  taskSpec: RawArtifactRecord;
  runConfig: RawArtifactRecord;
  initialState: RawArtifactRecord;
  finalState: RawArtifactRecord;
  trace: TraceEvent[];
  runResult: RunResult;
  verifierResult: VerifierResult;
  attributionResult: AttributionResult;
  failureCard: FailureCard;
  repairPackage: RepairPackage;
  regressionArtifact: RegressionArtifact;
  artifactNames: typeof REFUND_FAILURE_ARTIFACT_NAMES;
}

const loadTrace = (): TraceEvent[] => {
  const raw = readFileSync(
    join(
      process.cwd(),
      "src",
      "fixtures",
      "refund-failure",
      "trace.jsonl",
    ),
    "utf8",
  )
    .trim()
    .split("\n")
    .map((line) => JSON.parse(line) as RawTraceEvent);

  return parseTrace(raw);
};

export const loadRefundFailureFixture = (): RefundFailureFixture => ({
  taskSpec: rawTaskSpec as unknown as RawArtifactRecord,
  runConfig: rawRunConfig as unknown as RawArtifactRecord,
  initialState: rawInitialState as unknown as RawArtifactRecord,
  finalState: rawFinalState as unknown as RawArtifactRecord,
  trace: loadTrace(),
  runResult: parseRunResult(rawRunResult as unknown as RawRunResult),
  verifierResult: parseVerifierResult(
    rawVerifierResult as unknown as RawVerifierResult,
  ),
  attributionResult: parseAttributionResult(
    rawAttributionResult as unknown as RawAttributionResult,
  ),
  failureCard: parseFailureCard(rawFailureCard as unknown as RawFailureCard),
  repairPackage: parseRepairPackage(
    rawRepairPackage as unknown as RawRepairPackage,
  ),
  regressionArtifact: parseRegressionArtifact(
    rawRegressionArtifact as unknown as RawRegressionArtifact,
  ),
  artifactNames: REFUND_FAILURE_ARTIFACT_NAMES,
});
