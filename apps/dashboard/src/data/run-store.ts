/**
 * Low-level filesystem access over the `runs/{run_id}/` artifact layout.
 *
 * Mirrors `src/trace_harness/tracing/artifact_store.py` — the filenames
 * below must stay in sync with that module, which is the source of truth for
 * this layout (renaming a file there is a breaking change here).
 */

import { existsSync, readFileSync } from "node:fs";
import path from "node:path";

import { resolveRunsDir } from "@/data/runs-dir";

export const ARTIFACT_NAMES = {
  taskSpec: "task_spec.json",
  runConfig: "run_config.json",
  initialState: "initial_state.json",
  trace: "trace.jsonl",
  finalState: "final_state.json",
  runResult: "run_result.json",
  verifierResult: "verifier_result.json",
  attributionResult: "attribution_result.json",
  failureCard: "failure_card.json",
  repairPackage: "repair_package.json",
  regressionArtifact: "regression_artifact.json",
} as const;

/** Runs-dir-level (not per-run): a derived, rebuildable index of all runs. */
export const RUN_INDEX_FILE = "index.json";

/** Thrown when a run id has no directory under the runs dir. */
export class RunNotFoundError extends Error {
  readonly runId: string;

  constructor(runId: string, runDirPath: string) {
    super(`run not found: '${runId}' (looked in ${runDirPath})`);
    this.name = "RunNotFoundError";
    this.runId = runId;
  }
}

/** Thrown when an artifact file exists but is not valid JSON. */
export class MalformedArtifactError extends Error {
  readonly runId: string;
  readonly fileName: string;

  constructor(runId: string, fileName: string, cause: unknown) {
    super(`malformed artifact '${fileName}' for run '${runId}': ${String(cause)}`);
    this.name = "MalformedArtifactError";
    this.runId = runId;
    this.fileName = fileName;
  }
}

export const runDir = (runId: string): string =>
  path.join(resolveRunsDir(), runId);

export const runExists = (runId: string): boolean => existsSync(runDir(runId));

export const artifactExists = (runId: string, fileName: string): boolean =>
  existsSync(path.join(runDir(runId), fileName));

/** Throws RunNotFoundError if the run directory doesn't exist. */
export const requireRun = (runId: string): void => {
  if (!runExists(runId)) {
    throw new RunNotFoundError(runId, runDir(runId));
  }
};

export const readArtifactJson = <T>(runId: string, fileName: string): T => {
  const raw = readFileSync(path.join(runDir(runId), fileName), "utf8");
  try {
    return JSON.parse(raw) as T;
  } catch (cause) {
    throw new MalformedArtifactError(runId, fileName, cause);
  }
};

export const readArtifactLines = (runId: string, fileName: string): string[] =>
  readFileSync(path.join(runDir(runId), fileName), "utf8")
    .trim()
    .split("\n")
    .filter((line) => line.length > 0);

/** One entry in the runs-dir-level index.json, wire shape (snake_case). */
export interface RawRunIndexEntry {
  run_id: string;
  task_id: string;
  status: string;
  termination_reason: string;
  steps_taken: number;
  started_at: string;
  finished_at: string;
  error: string | null;
  verifier_passed: boolean | null;
  failed_check_count: number | null;
  batch_id?: string | null;
}

/** Reads the runs-dir index; an absent index means no runs yet, not an error. */
export const readRunIndexEntries = (): RawRunIndexEntry[] => {
  const indexPath = path.join(resolveRunsDir(), RUN_INDEX_FILE);
  if (!existsSync(indexPath)) return [];
  let parsed: { entries: RawRunIndexEntry[] };
  try {
    parsed = JSON.parse(readFileSync(indexPath, "utf8")) as {
      entries: RawRunIndexEntry[];
    };
  } catch (cause) {
    throw new MalformedArtifactError("(runs index)", RUN_INDEX_FILE, cause);
  }
  return parsed.entries ?? [];
};
