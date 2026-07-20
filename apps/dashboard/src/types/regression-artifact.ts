/**
 * Regression-artifact data contract.
 *
 * Mirrors `RegressionArtifact` in `src/trace_harness/regression/schemas.py`
 * (REGRESSION_SCHEMA_VERSION 0.1.0), serialized as `regression_artifact.json`.
 *
 * Pins everything needed to re-test a failure class later: the task, initial
 * state, the exact docs the agent saw, the checks that must hold, and a replay
 * command. Also carries positive sibling tests, nearby scenarios that must keep
 * passing, so a fix cannot quietly overblock legitimate behavior.
 */

import { camelizeKeys, type Camelize } from "@/lib/casing";
import type { Severity } from "@/types/severity";

export const REGRESSION_SCHEMA_VERSION = "0.1.0";

/**
 * Wire shape of a positive companion scenario that must continue to pass,
 * defined in the backend Pydantic model (snake_case keys).
 */
export interface RawSiblingTest {
  test_name: string;
  task_fixture: string;
  description: string;
}

/** A positive companion scenario that must continue to pass, camelCase. */
export type SiblingTest = Camelize<RawSiblingTest>;

/**
 * Wire shape of `regression_artifact.json`, defined in the backend Pydantic
 * model (snake_case keys). To do: give the untyped `initial_state`,
 * `pinned_docs`, and `metadata` fields concrete shapes once the dashboard
 * settles which of their keys it renders (mirrors the enum TODO in
 * repair-package.ts).
 */
export interface RawRegressionArtifact {
  schema_version: string;
  test_name: string;
  source_run_id: string;
  /** Repo-relative path of the originating task fixture (replay input). */
  task_fixture: string;
  initial_state: Record<string, unknown>;
  /** The exact docs (content + status + metadata) the failing run saw. */
  pinned_docs: Record<string, unknown>[];
  expected_behavior: string[];
  forbidden_actions: string[];
  /** Verifier check ids that must hold when this regression is replayed. */
  verifier_checks: string[];
  positive_sibling_tests: RawSiblingTest[];
  severity: Severity;
  blocks_release: boolean;
  replay_command: string;
  metadata: Record<string, unknown>;
}

/** "How to re-test this failure class later", the camelCase domain type. */
export type RegressionArtifact = Camelize<RawRegressionArtifact>;

export const parseRegressionArtifact = (
  raw: RawRegressionArtifact,
): RegressionArtifact => camelizeKeys(raw);
