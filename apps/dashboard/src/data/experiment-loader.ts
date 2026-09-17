/**
 * Loads experiments from the runs directory.
 *
 * Mirrors `RunReader.list_experiments` / `RunReader.get_experiment`, with the
 * same missing-artifact semantics as `run-loader.ts`:
 *   - unknown experiment id                 -> throws ExperimentNotFoundError
 *   - a plan with no result recorded yet    -> result is null
 *   - malformed JSON on an existing file    -> throws MalformedArtifactError
 *
 * An experiment lives beside the batches it compares rather than inside any
 * one of them, because it is the thing that relates several batches.
 */

import { existsSync, readdirSync, readFileSync } from "node:fs";
import path from "node:path";

import { MalformedArtifactError } from "@/data/run-store";
import { resolveRunsDir } from "@/data/runs-dir";
import {
  parseExperimentResult,
  parseExperimentSpec,
  type ExperimentResult,
  type ExperimentSpec,
  type RawExperimentResult,
  type RawExperimentSpec,
} from "@/types/experiment";

export const EXPERIMENTS_DIR = "experiments";
export const EXPERIMENT_SPEC_FILE = "experiment.json";
export const EXPERIMENT_RESULT_FILE = "result.json";

/** Thrown when an experiment id has no plan on disk. */
export class ExperimentNotFoundError extends Error {
  readonly experimentId: string;

  constructor(experimentId: string) {
    super(`experiment '${experimentId}' not found`);
    this.name = "ExperimentNotFoundError";
    this.experimentId = experimentId;
  }
}

const experimentsRoot = (): string =>
  path.join(resolveRunsDir(), EXPERIMENTS_DIR);

const experimentDir = (experimentId: string): string =>
  path.join(experimentsRoot(), experimentId);

const readJson = <T>(filePath: string, experimentId: string): T => {
  try {
    return JSON.parse(readFileSync(filePath, "utf8")) as T;
  } catch (cause) {
    throw new MalformedArtifactError(
      experimentId,
      path.basename(filePath),
      cause,
    );
  }
};

/** Every experiment plan on disk, oldest first by id. */
export const listExperiments = (): ExperimentSpec[] => {
  const root = experimentsRoot();
  if (!existsSync(root)) return [];

  return readdirSync(root, { withFileTypes: true })
    .filter(
      (entry) =>
        entry.isDirectory() &&
        existsSync(path.join(root, entry.name, EXPERIMENT_SPEC_FILE)),
    )
    .map((entry) => entry.name)
    .sort()
    .map((experimentId) =>
      parseExperimentSpec(
        readJson<RawExperimentSpec>(
          path.join(experimentDir(experimentId), EXPERIMENT_SPEC_FILE),
          experimentId,
        ),
      ),
    );
};

/**
 * The plan and, when a result has been recorded, what came back. The result is
 * null while conditions are still running, which is a normal state rather than
 * an error.
 */
export const getExperiment = (
  experimentId: string,
): { spec: ExperimentSpec; result: ExperimentResult | null } => {
  const specPath = path.join(experimentDir(experimentId), EXPERIMENT_SPEC_FILE);
  if (!existsSync(specPath)) throw new ExperimentNotFoundError(experimentId);

  const spec = parseExperimentSpec(
    readJson<RawExperimentSpec>(specPath, experimentId),
  );

  const resultPath = path.join(
    experimentDir(experimentId),
    EXPERIMENT_RESULT_FILE,
  );
  const result = existsSync(resultPath)
    ? parseExperimentResult(
        readJson<RawExperimentResult>(resultPath, experimentId),
      )
    : null;

  return { spec, result };
};
