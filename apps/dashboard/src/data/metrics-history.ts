/**
 * Reads the retained metrics history off disk.
 *
 * `docs/acceptance/metrics_history.jsonl` is committed, so the page renders
 * with no network and no backend. #205's uploader mirrors the same file to
 * Supabase; when that lands this stays the offline fallback rather than being
 * replaced, because a dashboard that needs a service to show a committed file
 * is worse than one that does not.
 */

import fs from "node:fs";
import path from "node:path";

import {
  parseMetricsHistory,
  type MetricsSnapshot,
} from "@/types/metrics-snapshot";

const REPO_ROOT = path.resolve(process.cwd(), "..", "..");

export const resolveMetricsHistoryPath = (): string => {
  const fromEnv = process.env.TRACE_METRICS_HISTORY;
  if (!fromEnv)
    return path.join(REPO_ROOT, "docs", "acceptance", "metrics_history.jsonl");
  return path.isAbsolute(fromEnv)
    ? fromEnv
    : path.resolve(process.cwd(), fromEnv);
};

/** Every snapshot in the history, oldest first. Missing file reads as empty. */
export const loadMetricsHistory = (): MetricsSnapshot[] => {
  const file = resolveMetricsHistoryPath();
  if (!fs.existsSync(file)) return [];
  return parseMetricsHistory(fs.readFileSync(file, "utf8"));
};
