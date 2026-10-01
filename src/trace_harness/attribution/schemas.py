"""Schemas for attribution results.

Attribution answers "where did this go wrong, and why" — *after* the
verifier has already decided that it went wrong. The step fields below are
deliberately separate because they are different concepts that must never
be collapsed (see docs/attribution_methodology.md):

    root_cause_step                 where the failure causally began
                                    (e.g. step 3: committing to the
                                    deprecated policy)
    first_bad_step                  earliest detectably-wrong step (may
                                    precede the root-cause commitment)
    missed_recovery_step            a step where the agent had the evidence
                                    to recover and didn't
    first_unrecoverable_step        earliest step after which no recovery
                                    path existed
    first_irreversible_action_step  first external action that cannot be
                                    taken back (e.g. step 5: cash refund)
    visible_symptom_steps           where the failure is externally
                                    observable (refund, false ticket, ...)

In the refund fixture, root cause is step 3 and the first irreversible
action is step 5 — two different steps, two different fields.

Two further fields describe a control block rather than the failure's cause
(0.4.0, #157): ``block_step`` is the first step an installed control blocked,
and ``post_block_outcome`` labels what the agent did after it. Outcome labels
are not failure categories; docs/failure_taxonomy.md keeps the two apart.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

ATTRIBUTION_SCHEMA_VERSION = "0.4.0"  # 0.4.0: block_step, post_block_outcome


class FailureCategory(StrEnum):
    """Coarse failure taxonomy for the MVP.

    Expected to evolve into a curated, human-labeled taxonomy (Darrel +
    Justin). Add values; do not repurpose existing ones. See
    docs/failure_taxonomy.md for the full taxonomy (definitions, the
    11-category scope these values cover, the additional categories added
    from published agent-failure research, and worked examples).
    """

    CLARIFICATION_FAILURE = "clarification_failure"
    PLANNING_ERROR = "planning_error"
    QUERY_FORMATION_ERROR = "query_formation_error"
    RETRIEVAL_SELECTION_ERROR = "retrieval_selection_error"
    STALE_SOURCE_AUTHORITY = "stale_source_authority"
    REASONING_COMMITMENT_ERROR = "reasoning_commitment_error"
    STATE_TRACKING_ERROR = "state_tracking_error"
    MISSED_RECOVERY = "missed_recovery"
    TOOL_SELECTION_ERROR = "tool_selection_error"
    UNSAFE_IRREVERSIBLE_ACTION = "unsafe_irreversible_action"
    TOOL_IMPLEMENTATION_ERROR = "tool_implementation_error"
    GROUNDING_CITATION_ERROR = "grounding_citation_error"
    CONTEXT_MEMORY_LEAKAGE = "context_memory_leakage"
    FALSE_DURABLE_RECORD = "false_durable_record"
    INCONSISTENT_FINAL_ANSWER = "inconsistent_final_answer"
    POLICY_VIOLATION = "policy_violation"
    PREMATURE_TERMINATION = "premature_termination"
    UNPRODUCTIVE_LOOP = "unproductive_loop"
    UNKNOWN = "unknown"


class PostBlockOutcome(StrEnum):
    """What the agent did after a control first blocked it (#157).

    One label per run, chosen by ``attribution.post_block.classify_post_block_outcome``
    in a fixed order when several apply. See docs/failure_taxonomy.md for the
    check-to-label map and why these are not failure categories.
    """

    RECOVERED = "recovered"
    SUBSTITUTE_VIOLATION = "substitute_violation"
    FALSE_SUCCESS = "false_success"
    UNSUPPORTED_CLAIM = "unsupported_claim"
    OVER_ESCALATION = "over_escalation"
    STALLED = "stalled"
    NO_BLOCK_OBSERVED = "no_block_observed"


class AttributionResult(BaseModel):
    schema_version: str = ATTRIBUTION_SCHEMA_VERSION
    run_id: str
    root_cause_step: int | None = None
    first_bad_step: int | None = None
    missed_recovery_step: int | None = None
    first_unrecoverable_step: int | None = None
    first_irreversible_action_step: int | None = None
    visible_symptom_steps: list[int] = Field(default_factory=list)
    primary_failure_category: FailureCategory = FailureCategory.UNKNOWN
    contributing_failure_categories: list[FailureCategory] = Field(default_factory=list)
    causal_explanation: str = ""
    evidence_step_ids: list[int] = Field(default_factory=list)
    # 0..1; heuristic attributions should stay well below 1.0 by policy.
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    ambiguity_notes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Both None on files written before 0.4.0, which never classified a block.
    block_step: int | None = None
    post_block_outcome: PostBlockOutcome | None = None
