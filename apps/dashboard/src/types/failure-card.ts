/**
 * Failure-card data contract.
 *
 * Mirrors `FailureCard` in `src/trace_harness/failure_bundles/schemas.py`
 * (FAILURE_CARD_SCHEMA_VERSION 0.2.0)
 * The human-readable artifact: what broke, where, how bad, with evidence
 */

import { camelizeKeys, type Camelize } from "@/lib/casing";
import type { RawEvidenceItem } from "@/types/evidence";
import type { Severity } from "@/types/severity";

export const FAILURE_CARD_SCHEMA_VERSION = "0.2.0";

/**
 * Wire shape of `failure_card.json`, defined in the backend Pydantic model
 */
export interface RawFailureCard {
  schema_version: string;
  run_id: string;
  task_id: string;
  title: string;
  summary: string;
  task_result: string;
  severity: Severity;
  root_cause: string;
  contributing_failures: string[];
  step_ids: number[];
  visible_symptoms: string[];
  evidence: RawEvidenceItem[];
  causal_explanation: string;
  blast_radius: string;
  metadata: Record<string, unknown>;
}

/** Human-readable summary of one verified failure, the camelCase domain type. */
export type FailureCard = Camelize<RawFailureCard>;

export const parseFailureCard = (raw: RawFailureCard): FailureCard =>
  camelizeKeys(raw);
