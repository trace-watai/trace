"""Deterministic consistency checks for attribution results.

This validation layer checks an already-constructed :class:`AttributionResult`
against the immutable trace and authoritative verifier result that produced it.
It does not infer attribution, change a verdict, or mutate any input.
"""

from __future__ import annotations

from dataclasses import dataclass

from trace_harness.attribution.schemas import AttributionResult
from trace_harness.tracing.events import TraceEvent
from trace_harness.verifiers.base import VerifierResult


@dataclass(frozen=True)
class AttributionValidationIssue:
    """One consistency problem in an attribution result."""

    code: str
    message: str


_SCALAR_STEP_FIELDS = (
    "root_cause_step",
    "first_bad_step",
    "missed_recovery_step",
    "first_unrecoverable_step",
    "first_irreversible_action_step",
)
_LIST_STEP_FIELDS = ("visible_symptom_steps", "evidence_step_ids")


def validate_attribution_result(
    attribution: AttributionResult,
    trace: list[TraceEvent],
    verifier_result: VerifierResult,
) -> list[AttributionValidationIssue]:
    """Return deterministic consistency issues (empty means valid).

    The verifier remains authoritative: attribution is valid only for a failed
    verdict, and validation never changes ``verifier_result.passed``.
    """
    issues: list[AttributionValidationIssue] = []

    if attribution.run_id != verifier_result.run_id:
        issues.append(
            AttributionValidationIssue(
                "attribution_verifier_run_id_mismatch",
                f"attribution run_id {attribution.run_id!r} does not match "
                f"verifier run_id {verifier_result.run_id!r}",
            )
        )

    expected_run_id = verifier_result.run_id
    mismatched_trace_events = [event.event_id for event in trace if event.run_id != expected_run_id]
    if mismatched_trace_events:
        issues.append(
            AttributionValidationIssue(
                "trace_run_id_mismatch",
                f"trace event(s) {mismatched_trace_events} do not match "
                f"verifier run_id {expected_run_id!r}",
            )
        )

    trace_event_ids = {event.event_id for event in trace}
    missing_parent_links = {
        event.parent_event_id
        for event in trace
        if event.parent_event_id is not None and event.parent_event_id not in trace_event_ids
    }
    if missing_parent_links:
        issues.append(
            AttributionValidationIssue(
                "trace_parent_event_not_found",
                f"trace parent_event_id reference(s) {sorted(missing_parent_links)} "
                "do not exist in the same trace",
            )
        )

    if verifier_result.passed:
        issues.append(
            AttributionValidationIssue(
                "attribution_for_passed_verdict",
                "attribution requires a failed verifier verdict; the verifier passed",
            )
        )

    trace_step_ids = {event.step_id for event in trace if event.step_id is not None}
    for field_name in _SCALAR_STEP_FIELDS:
        step_id = getattr(attribution, field_name)
        if step_id is not None and step_id not in trace_step_ids:
            issues.append(_unknown_step_issue(field_name, step_id))
    for field_name in _LIST_STEP_FIELDS:
        for step_id in getattr(attribution, field_name):
            if step_id not in trace_step_ids:
                issues.append(_unknown_step_issue(field_name, step_id))

    semantic_steps = {
        step_id
        for field_name in _SCALAR_STEP_FIELDS
        if (step_id := getattr(attribution, field_name)) is not None
    }
    semantic_steps.update(attribution.visible_symptom_steps)
    missing_evidence_steps = sorted(semantic_steps - set(attribution.evidence_step_ids))
    if missing_evidence_steps:
        issues.append(
            AttributionValidationIssue(
                "semantic_step_missing_from_evidence",
                f"attribution step(s) {missing_evidence_steps} are not included in "
                "evidence_step_ids",
            )
        )

    failed_check_steps = {
        step_id for check in verifier_result.failed_checks for step_id in check.step_ids
    }
    unsupported_symptoms = sorted(set(attribution.visible_symptom_steps) - failed_check_steps)
    if unsupported_symptoms:
        issues.append(
            AttributionValidationIssue(
                "symptom_step_not_backed_by_failed_check",
                f"visible symptom step(s) {unsupported_symptoms} are not referenced "
                "by any failed verifier check",
            )
        )

    verifier_step_references = {
        step_id for check in verifier_result.failed_checks for step_id in check.step_ids
    }
    verifier_step_references.update(
        step_id
        for check in verifier_result.failed_checks
        for evidence in check.evidence
        for step_id in evidence.step_ids
    )
    verifier_step_references.update(
        step_id for evidence in verifier_result.evidence for step_id in evidence.step_ids
    )
    missing_verifier_steps = sorted(verifier_step_references - trace_step_ids)
    if missing_verifier_steps:
        issues.append(
            AttributionValidationIssue(
                "verifier_evidence_step_not_in_trace",
                f"verifier evidence references step(s) {missing_verifier_steps} "
                "that do not exist in the trace",
            )
        )

    return issues


def _unknown_step_issue(field_name: str, step_id: int) -> AttributionValidationIssue:
    return AttributionValidationIssue(
        "attribution_step_not_in_trace",
        f"{field_name} references step {step_id}, which does not exist in the trace",
    )
