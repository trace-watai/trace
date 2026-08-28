/**
 * Typed reads over a single `runs/{run_id}` directory — the TypeScript
 * counterpart to `trace_harness.run_reader.RunReader`
 * (`src/trace_harness/run_reader.py`), read directly off disk instead of
 * through the fixture bundle in `@/data/refund-failure-fixture`.
 *
 * Method-for-method mirror so pending dashboard views (trace, verifier,
 * attribution, repair) can extend this without rework, and so a future API
 * server (see docs/future_api.md) only ever requires changing the body of
 * these functions, never the call sites.
 *
 * Missing-artifact states, same semantics as `RunReader`:
 *   - unknown run id                          -> throws RunNotFoundError
 *   - a not-yet-produced downstream artifact   -> returns null
 *     (verifier/attribution before those stages run; bundle before `bundle`)
 *   - malformed JSON on an existing artifact   -> throws MalformedArtifactError
 */

import {
  ARTIFACT_NAMES,
  artifactExists,
  readArtifactJson,
  readArtifactLines,
  readRunIndexEntries,
  requireRun,
  type RawRunIndexEntry,
} from "@/data/run-store";
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
  parseRepairPackage,
  type RawRepairPackage,
  type RepairPackage,
} from "@/types/repair-package";
import {
  parseRegressionArtifact,
  type RawRegressionArtifact,
  type RegressionArtifact,
} from "@/types/regression-artifact";
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

export { RunNotFoundError, MalformedArtifactError } from "@/data/run-store";

/** The subset of `task_spec.json` the dashboard currently reads. */
export interface TaskSpecSummary {
  taskId: string;
  title: string;
  goal: string;
}

/** One-line summary of a run for listing, mirrors RunReader.RunSummary. */
export interface RunSummary {
  runId: string;
  taskId: string;
  status: string;
  terminationReason: string;
  stepsTaken: number;
  startedAt: string;
  finishedAt: string;
  error: string | null;
  verifierPassed: boolean | null;
  failedCheckCount: number | null;
  batchId: string | null;
}

const toRunSummary = (entry: RawRunIndexEntry): RunSummary => ({
  runId: entry.run_id,
  taskId: entry.task_id,
  status: entry.status,
  terminationReason: entry.termination_reason,
  stepsTaken: entry.steps_taken,
  startedAt: entry.started_at,
  finishedAt: entry.finished_at,
  error: entry.error,
  verifierPassed: entry.verifier_passed,
  failedCheckCount: entry.failed_check_count,
  batchId: entry.batch_id ?? null,
});

/** Summaries of every listed run, oldest-first. Empty if no runs yet. */
export const listRuns = (): RunSummary[] =>
  readRunIndexEntries().map(toRunSummary);

export const getRun = (runId: string): RunResult => {
  requireRun(runId);
  return parseRunResult(
    readArtifactJson<RawRunResult>(runId, ARTIFACT_NAMES.runResult),
  );
};

export const getTask = (runId: string): TaskSpecSummary => {
  requireRun(runId);
  const raw = readArtifactJson<{ task_id: string; title: string; goal: string }>(
    runId,
    ARTIFACT_NAMES.taskSpec,
  );
  return { taskId: raw.task_id, title: raw.title, goal: raw.goal };
};

export const getTrace = (runId: string): TraceEvent[] => {
  requireRun(runId);
  const rawEvents = readArtifactLines(runId, ARTIFACT_NAMES.trace).map(
    (line) => JSON.parse(line) as RawTraceEvent,
  );
  return parseTrace(rawEvents);
};

export const getVerifier = (runId: string): VerifierResult | null => {
  requireRun(runId);
  if (!artifactExists(runId, ARTIFACT_NAMES.verifierResult)) return null;
  return parseVerifierResult(
    readArtifactJson<RawVerifierResult>(runId, ARTIFACT_NAMES.verifierResult),
  );
};

export const getAttribution = (runId: string): AttributionResult | null => {
  requireRun(runId);
  if (!artifactExists(runId, ARTIFACT_NAMES.attributionResult)) return null;
  return parseAttributionResult(
    readArtifactJson<RawAttributionResult>(
      runId,
      ARTIFACT_NAMES.attributionResult,
    ),
  );
};

export interface FailureBundle {
  failureCard: FailureCard;
  repairPackage: RepairPackage;
  regressionArtifact: RegressionArtifact;
}

/**
 * The three bundle artifacts, or null if the run hasn't been bundled yet.
 * The `bundle` stage writes all three together, so a partially-written
 * bundle (crash mid-stage) surfaces as a MalformedArtifactError /
 * ENOENT-style read failure rather than a silently half-built bundle.
 */
export const getBundle = (runId: string): FailureBundle | null => {
  requireRun(runId);
  if (!artifactExists(runId, ARTIFACT_NAMES.failureCard)) return null;
  return {
    failureCard: parseFailureCard(
      readArtifactJson<RawFailureCard>(runId, ARTIFACT_NAMES.failureCard),
    ),
    repairPackage: parseRepairPackage(
      readArtifactJson<RawRepairPackage>(runId, ARTIFACT_NAMES.repairPackage),
    ),
    regressionArtifact: parseRegressionArtifact(
      readArtifactJson<RawRegressionArtifact>(
        runId,
        ARTIFACT_NAMES.regressionArtifact,
      ),
    ),
  };
};
