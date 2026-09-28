/**
 * Failure-card data contract.
 *
 * Mirrors `FailureCard` in `src/trace_harness/failure_bundles/schemas.py`
 * (FAILURE_CARD_SCHEMA_VERSION 0.5.0)
 * The human-readable artifact: what broke, where, how bad, with evidence
 */

import { camelizeKeys, type Camelize } from "@/lib/casing";
import type { FailureCategory } from "@/types/attribution";
import type { RawEvidenceItem } from "@/types/evidence";
import type { Severity } from "@/types/severity";

export const FAILURE_CARD_SCHEMA_VERSION = "0.5.0";

/**
 * Wire shape of the `blast_radius` object (0.4.0): the structured scope of
 * external impact, computed from final state — money moved, records created,
 * customers affected. `summary` is the pre-formatted plain-text fallback.
 */
export interface RawBlastRadius {
  refund_count: number;
  refund_total_usd: number;
  ticket_count: number;
  escalation_count: number;
  customers_affected: string[];
  summary: string;
}

/** Structured blast radius, the camelCase domain type. */
export type BlastRadius = Camelize<RawBlastRadius>;

/**
 * One failing run a card covers (0.5.0, #211). The first is the run the card
 * was generated from, and every later one reproduced the same bundle key.
 */
export interface RawBundleOccurrence {
  run_id: string;
  task_id: string;
  provider: string | null;
  model: string | null;
  seed: number | null;
}

export type BundleOccurrence = Camelize<RawBundleOccurrence>;

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
  /** Coarse categories that contributed, primary first (closed vocabulary). */
  contributing_failures: FailureCategory[];
  step_ids: number[];
  visible_symptoms: string[];
  evidence: RawEvidenceItem[];
  causal_explanation: string;
  blast_radius: RawBlastRadius;
  metadata: Record<string, unknown>;
  /** Root-cause identity (0.5.0); absent or null on older cards. */
  bundle_key?: string | null;
  /** Runs this card covers, its own run first (0.5.0); absent on older cards. */
  occurrences?: RawBundleOccurrence[];
}

/**
 * Human-readable summary of one verified failure, the camelCase domain type.
 * A card written before 0.5.0 parses with no key and no occurrences.
 */
export type FailureCard = Omit<
  Camelize<RawFailureCard>,
  "bundleKey" | "occurrences"
> & {
  bundleKey: string | null;
  occurrences: BundleOccurrence[];
};

export const parseFailureCard = (raw: RawFailureCard): FailureCard => ({
  ...camelizeKeys(raw),
  bundleKey: raw.bundle_key ?? null,
  occurrences: camelizeKeys(raw.occurrences ?? []),
});

/**
 * Wire shape of `bundle_ref.json` (0.1.0): what a reproduction's run directory
 * holds instead of its own bundle, naming the run whose directory has the card.
 */
export interface RawBundleRef {
  schema_version: string;
  run_id: string;
  task_id: string;
  bundle_key: string;
  canonical_run_id: string;
}
