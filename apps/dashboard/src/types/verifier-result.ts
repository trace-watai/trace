/**
 * Verifier-result data contract.
 *
 * Mirrors `VerifierResult` in `src/trace_harness/verifiers/base.py`
 * (VERIFIER_RESULT_SCHEMA_VERSION 0.3.0), serialized as `verifier_result.json`.
 *
 * The verdict for one run from one verifier (or a merge of several). The
 * verifier is the authority on pass/fail: it returns structured evidence and
 * itemized failed checks, never a bare boolean. This is the artifact that
 * feeds the failure card.
 */

import { camelizeKeys, type Camelize } from "@/lib/casing";
import type { RawEvidenceItem } from "@/types/evidence";
import type { Severity } from "@/types/severity";

export const VERIFIER_RESULT_SCHEMA_VERSION = "0.3.0";

/**
 * Wire shape of one deterministic check that failed, defined in the backend
 * Pydantic model (snake_case keys).
 */
export interface RawFailedCheck {
  check_id: string;
  message: string;
  expected: string;
  actual: string;
  step_ids: number[];
  evidence: RawEvidenceItem[];
  severity: Severity;
  blocks_release: boolean;
}

/** One deterministic check that failed, the camelCase domain type. */
export type FailedCheck = Camelize<RawFailedCheck>;

/**
 * Wire shape of `verifier_result.json`, defined in the backend Pydantic model
 * (snake_case keys).
 */
export interface RawVerifierResult {
  schema_version: string;
  verifier_id: string;
  run_id: string;
  passed: boolean;
  failed_checks: RawFailedCheck[];
  warnings: string[];
  /** Highest severity among failed checks; `null` when passed. */
  severity: Severity | null;
  blocks_release: boolean;
  /** Run-level evidence not tied to a single check (e.g. retrieval provenance). */
  evidence: RawEvidenceItem[];
  metadata: Record<string, unknown>;
}

/** "Did this run pass, and if not why", the camelCase domain type. */
export type VerifierResult = Camelize<RawVerifierResult>;

export const parseVerifierResult = (raw: RawVerifierResult): VerifierResult =>
  camelizeKeys(raw);
