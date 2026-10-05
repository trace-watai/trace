import { EvidenceKind } from "@/types/evidence";

const EVIDENCE_KIND_LABELS: Record<EvidenceKind, string> = {
  [EvidenceKind.ORDER_RECORD]: "Order record",
  [EvidenceKind.REFUND_RECORD]: "Refund record",
  [EvidenceKind.TICKET_RECORD]: "Ticket record",
  [EvidenceKind.ESCALATION_RECORD]: "Escalation record",
  [EvidenceKind.POLICY_RULES]: "Policy rules",
  [EvidenceKind.RETRIEVAL_PROVENANCE]: "Retrieved documents",
  [EvidenceKind.PROVENANCE_QUOTE]: "Cited source",
  [EvidenceKind.FINAL_ANSWER]: "Final answer",
};

/** Human-facing name for an evidence kind; sentence-cases anything unrecognized. */
export const evidenceKindLabel = (kind: string): string =>
  EVIDENCE_KIND_LABELS[kind as EvidenceKind] ??
  kind.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
