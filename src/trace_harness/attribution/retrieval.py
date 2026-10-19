"""Conservative retrieval attribution, with explicit limits per failed check."""

from trace_harness.attribution.post_block import classify_post_block_outcome
from trace_harness.attribution.schemas import AttributionResult, FailureCategory
from trace_harness.runner.result import RunResult
from trace_harness.tracing.events import TraceEvent
from trace_harness.verifiers.base import VerifierResult

CHECK_CATEGORIES = {
    "retrieval_wrong_document": FailureCategory.RETRIEVAL_SELECTION_ERROR,
    "retrieval_unsupported_claim": FailureCategory.GROUNDING_CITATION_ERROR,
    "retrieval_citation_unsupported": FailureCategory.GROUNDING_CITATION_ERROR,
    "retrieval_stale_document": FailureCategory.STALE_SOURCE_AUTHORITY,
    "retrieval_conflict_ignored": FailureCategory.REASONING_COMMITMENT_ERROR,
    "retrieval_insufficient_evidence": FailureCategory.CLARIFICATION_FAILURE,
    "retrieval_answer_invalid": FailureCategory.GROUNDING_CITATION_ERROR,
    "retrieval_answer_incomplete": FailureCategory.PREMATURE_TERMINATION,
}


def attribute_retrieval(
    trace: list[TraceEvent], verifier: VerifierResult, run: RunResult | None
) -> AttributionResult:
    steps = sorted({s for check in verifier.failed_checks for s in check.step_ids})
    categories = list(
        dict.fromkeys(
            CHECK_CATEGORIES.get(c.check_id, FailureCategory.UNKNOWN)
            for c in verifier.failed_checks
        )
    )
    block = classify_post_block_outcome(trace, verifier, run)
    return AttributionResult(
        run_id=verifier.run_id,
        first_bad_step=steps[0] if steps else None,
        visible_symptom_steps=steps,
        evidence_step_ids=steps,
        primary_failure_category=categories[0] if categories else FailureCategory.UNKNOWN,
        contributing_failure_categories=categories[1:],
        confidence=0.35 if steps else 0.0,
        causal_explanation=(
            "The standing answer violates the declared retrieval evidence contract. "
            "The verifier localizes the observed symptoms; the trace does not establish "
            "which earlier selection or reasoning decision caused them."
        ),
        ambiguity_notes=[
            f"{check.check_id}: root_cause_step is null because the standing answer "
            "proves a violation, not the causal origin of the agent's source selection."
            for check in verifier.failed_checks
        ]
        + [
            "missed_recovery_step is null: no retrieval-domain recovery detector is validated.",
            "No external side effects exist in this read-only workflow; irreversible and "
            "unrecoverable steps are null.",
        ],
        metadata={"attributor": "heuristic", "workflow_type": "retrieval.answer"},
        block_step=block.block_step,
        post_block_outcome=block.outcome,
    )
