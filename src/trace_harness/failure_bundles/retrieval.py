"""Repair templates for the retrieval workflow's public check ids."""

from trace_harness.failure_bundles.schemas import ControlPriority, RepairControl


def retrieval_evidence_control(checks: list[str]) -> RepairControl:
    return RepairControl(
        name="retrieval_evidence_contract",
        installation_point="RetrievalEnvironment.check_final_answer (final-answer seam)",
        check=(
            "Validate structured atomic claims against observed current section text, "
            "citation bounds, the task's required claims, and conflicting evidence."
        ),
        behavior_on_failure="Reject the answer and request supported citations or an abstention.",
        expected_impact="Prevent unsupported, incomplete or ungrounded retrieval answers.",
        why_it_prevents_recurrence="The answer must supply the evidence its claims depend on.",
        risk_or_tradeoff=(
            "Not materializable yet. The oracle handles declared atomic facts, not "
            "arbitrary paraphrases; extending it requires new labeled corpus facts."
        ),
        priority=ControlPriority.P1,
        linked_verifier_checks=checks,
    )


def retrieval_stale_control(checks: list[str]) -> RepairControl:
    return RepairControl(
        name="retrieval_current_section_precedence",
        installation_point="RetrievalEnvironment.check_final_answer (final-answer seam)",
        check=(
            "For every asserted claim, block a deprecated section citation when a "
            "current section declaring that claim was already retrieved."
        ),
        behavior_on_failure="Block the proposed final answer and request current citations.",
        expected_impact="Eliminate reliance on deprecated sections when current evidence exists.",
        why_it_prevents_recurrence=(
            "Retrieved current evidence takes precedence at the answer seam."
        ),
        risk_or_tradeoff=(
            "A blocked recording has no next move. Supply an explicit continuation to "
            "exercise recovery; scripted acceptance remains advisory, not live evidence."
        ),
        priority=ControlPriority.P0,
        linked_verifier_checks=checks,
    )
