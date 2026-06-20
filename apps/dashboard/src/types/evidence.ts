/**
 * Evidence-item data contract.
 *
 * Mirrors `EvidenceItem` in `src/trace_harness/verifiers/base.py` 
 * One piece of evidence backing a check outcome or failure card 
 * Shared by verifier results, failed checks, and failure cards
 */

import { camelizeKeys, type Camelize } from "@/lib/casing";

/**
 * Raw shape of one evidence item, defined in the backend Pydantic model
 */
export interface RawEvidenceItem {
  // To Do: Transform Kind to an enum after backend provides values
  kind: string;
  description: string;
  step_ids: number[];
  data: Record<string, unknown>;
}

/** One piece of evidence supporting a check or failure card, camelCase. */
export type EvidenceItem = Camelize<RawEvidenceItem>;

export const parseEvidenceItem = (raw: RawEvidenceItem): EvidenceItem =>
  camelizeKeys(raw);
