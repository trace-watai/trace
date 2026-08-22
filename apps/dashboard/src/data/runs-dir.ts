/**
 * Resolves the `runs/` directory the dashboard reads from.
 *
 * Mirrors the `TRACE_RUNS_DIR` convention in
 * `src/trace_harness/config.py` (`HarnessConfig.from_env`) so the harness and
 * the dashboard can be pointed at the same directory. An empty-string env
 * value falls back to the default, same as the Python side.
 */

import path from "node:path";

const REPO_ROOT = path.resolve(process.cwd(), "..", "..");
const DEFAULT_RUNS_DIR = path.join(REPO_ROOT, "runs");

export const resolveRunsDir = (): string => {
  const fromEnv = process.env.TRACE_RUNS_DIR;
  if (!fromEnv) return DEFAULT_RUNS_DIR;
  // Relative values are resolved against the dashboard process's own cwd
  // (where `next dev`/`next start` runs from), not the repo root — same
  // convention Next.js uses for its own relative env values.
  return path.isAbsolute(fromEnv)
    ? fromEnv
    : path.resolve(process.cwd(), fromEnv);
};
