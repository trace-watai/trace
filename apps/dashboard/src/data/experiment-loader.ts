/**
 * Loads experiments from the runs directory.
 *
 * Mirrors `RunReader.list_experiments`, `unreadable_experiments` and
 * `get_experiment`, with the same missing-artifact semantics as
 * `run-loader.ts`:
 *   - unknown experiment id, or one that is
 *     not a single path segment             -> throws ExperimentNotFoundError
 *   - a plan with no result recorded yet    -> result is null
 *   - malformed JSON on an existing file, or
 *     a file naming another experiment      -> throws MalformedArtifactError
 *
 * `listExperiments` leaves out an experiment whose files do not load and
 * `listUnreadableExperiments` names it, so one bad file cannot hide the rest.
 *
 * An experiment lives beside the batches it compares rather than inside any
 * one of them, because it is the thing that relates several batches.
 */

import { existsSync, readdirSync, readFileSync } from "node:fs";
import path from "node:path";

import { MalformedArtifactError } from "@/data/run-store";
import { resolveRunsDir } from "@/data/runs-dir";
import {
  EXPERIMENT_ID_PATTERN,
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

const experimentIdsOnDisk = (): string[] => {
  const root = experimentsRoot();
  if (!existsSync(root)) return [];

  return readdirSync(root, { withFileTypes: true })
    .filter(
      (entry) =>
        entry.isDirectory() &&
        existsSync(path.join(root, entry.name, EXPERIMENT_SPEC_FILE)),
    )
    .map((entry) => entry.name)
    .sort();
};

/** Every experiment whose files load, oldest first by id. */
export const listExperiments = (): ExperimentSpec[] =>
  experimentIdsOnDisk().flatMap((experimentId) => {
    try {
      return [getExperiment(experimentId).spec];
    } catch {
      return [];
    }
  });

/** Experiments on disk whose plan or result does not load, and why. */
export const listUnreadableExperiments = (): {
  experimentId: string;
  error: string;
}[] =>
  experimentIdsOnDisk().flatMap((experimentId) => {
    try {
      getExperiment(experimentId);
      return [];
    } catch (error) {
      return [{ experimentId, error: String(error) }];
    }
  });

/**
 * The plan and, when a result has been recorded, what came back. The result is
 * null while conditions are still running, which is a normal state rather than
 * an error.
 */
export const getExperiment = (
  experimentId: string,
): { spec: ExperimentSpec; result: ExperimentResult | null } => {
  // An id that is not one path segment cannot name an experiment, and joining
  // it onto the experiments directory could read outside it.
  if (!EXPERIMENT_ID_PATTERN.test(experimentId)) {
    throw new ExperimentNotFoundError(experimentId);
  }
  const specPath = path.join(experimentDir(experimentId), EXPERIMENT_SPEC_FILE);
  if (!existsSync(specPath)) throw new ExperimentNotFoundError(experimentId);

  const spec = parseExperimentSpec(
    readJson<RawExperimentSpec>(specPath, experimentId),
  );
  requireOwnId(spec.experimentId, experimentId, EXPERIMENT_SPEC_FILE);

  const resultPath = path.join(
    experimentDir(experimentId),
    EXPERIMENT_RESULT_FILE,
  );
  const result = existsSync(resultPath)
    ? parseExperimentResult(
        readJson<RawExperimentResult>(resultPath, experimentId),
      )
    : null;
  if (result) {
    requireOwnId(result.experimentId, experimentId, EXPERIMENT_RESULT_FILE);
  }

  return { spec, result };
};

/** A file copied into another experiment's directory is not that experiment. */
const requireOwnId = (
  named: string,
  experimentId: string,
  fileName: string,
): void => {
  if (named !== experimentId) {
    throw new MalformedArtifactError(
      experimentId,
      fileName,
      `names experiment '${named}'`,
    );
  }
};
