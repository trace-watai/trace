"""Offline consistency tests for attribution validation."""

from __future__ import annotations

from trace_harness.attribution.schemas import AttributionResult
from trace_harness.attribution.validation import validate_attribution_result
from trace_harness.tasks.schemas import Severity
from trace_harness.tracing.events import TraceEvent, TraceEventType
from trace_harness.verifiers.base import (
    EvidenceItem,
    EvidenceKind,
    FailedCheck,
    VerifierResult,
)


def _event(event_id: str, step_id: int, *, run_id: str = "run_test") -> TraceEvent:
    return TraceEvent(
        event_id=event_id,
        run_id=run_id,
        step_id=step_id,
        event_type=TraceEventType.MODEL_ACTION,
        payload={"kind": "final_answer"},
    )


def _failed_verdict(
    *,
    run_id: str = "run_test",
    check_steps: list[int] | None = None,
    evidence_steps: list[int] | None = None,
) -> VerifierResult:
    return VerifierResult(
        verifier_id="test_verifier",
        run_id=run_id,
        passed=False,
        failed_checks=[
            FailedCheck(
                check_id="test_failure",
                message="verified failure",
                expected="safe behavior",
                actual="unsafe behavior",
                step_ids=check_steps or [],
                evidence=[
                    EvidenceItem(
                        kind=EvidenceKind.FINAL_ANSWER,
                        description="supporting evidence",
                        step_ids=evidence_steps or [],
                    )
                ],
                severity=Severity.HIGH,
            )
        ],
    )


def _codes(
    attribution: AttributionResult,
    trace: list[TraceEvent],
    verdict: VerifierResult,
) -> list[str]:
    return [issue.code for issue in validate_attribution_result(attribution, trace, verdict)]


def test_valid_attribution_has_no_issues() -> None:
    trace = [_event("evt_1", 1), _event("evt_2", 2), _event("evt_3", 3)]
    verdict = _failed_verdict(check_steps=[3], evidence_steps=[1])
    attribution = AttributionResult(
        run_id="run_test",
        root_cause_step=1,
        first_bad_step=1,
        missed_recovery_step=2,
        first_unrecoverable_step=3,
        first_irreversible_action_step=3,
        visible_symptom_steps=[3],
        evidence_step_ids=[1, 2, 3],
    )

    assert validate_attribution_result(attribution, trace, verdict) == []


def test_rejects_attribution_for_passed_verdict() -> None:
    verdict = VerifierResult(
        verifier_id="test_verifier",
        run_id="run_test",
        passed=True,
    )

    assert _codes(
        AttributionResult(run_id="run_test"),
        [_event("evt_1", 1)],
        verdict,
    ) == ["attribution_for_passed_verdict"]


def test_reports_attribution_and_trace_run_id_mismatches() -> None:
    issues = validate_attribution_result(
        AttributionResult(run_id="run_attribution"),
        [_event("evt_wrong", 1, run_id="run_other")],
        _failed_verdict(run_id="run_verifier"),
    )

    assert {issue.code for issue in issues} == {
        "attribution_verifier_run_id_mismatch",
        "trace_run_id_mismatch",
    }
    assert "evt_wrong" in next(
        issue.message for issue in issues if issue.code == "trace_run_id_mismatch"
    )


def test_parent_event_references_must_resolve_within_the_same_trace() -> None:
    event = _event("evt_child", 1)
    event.parent_event_id = "evt_missing_parent"

    issues = validate_attribution_result(
        AttributionResult(run_id="run_test"),
        [event],
        _failed_verdict(),
    )

    issue = next(issue for issue in issues if issue.code == "trace_parent_event_not_found")
    assert "evt_missing_parent" in issue.message


def test_reports_every_attribution_field_that_references_an_unknown_step() -> None:
    attribution = AttributionResult(
        run_id="run_test",
        root_cause_step=10,
        first_bad_step=11,
        missed_recovery_step=12,
        first_unrecoverable_step=13,
        first_irreversible_action_step=14,
        visible_symptom_steps=[15],
        evidence_step_ids=[10, 11, 12, 13, 14, 15],
    )

    issues = validate_attribution_result(
        attribution,
        [_event("evt_1", 1)],
        _failed_verdict(check_steps=[15]),
    )
    unknown_messages = [
        issue.message for issue in issues if issue.code == "attribution_step_not_in_trace"
    ]

    assert len(unknown_messages) == 12
    for field_name in (
        "root_cause_step",
        "first_bad_step",
        "missed_recovery_step",
        "first_unrecoverable_step",
        "first_irreversible_action_step",
        "visible_symptom_steps",
        "evidence_step_ids",
    ):
        assert any(field_name in message for message in unknown_messages)


def test_semantic_steps_must_be_in_attribution_evidence() -> None:
    attribution = AttributionResult(
        run_id="run_test",
        root_cause_step=1,
        missed_recovery_step=2,
        visible_symptom_steps=[3],
        evidence_step_ids=[1],
    )

    issues = validate_attribution_result(
        attribution,
        [_event("evt_1", 1), _event("evt_2", 2), _event("evt_3", 3)],
        _failed_verdict(check_steps=[3]),
    )

    issue = next(issue for issue in issues if issue.code == "semantic_step_missing_from_evidence")
    assert "[2, 3]" in issue.message


def test_visible_symptoms_must_be_backed_by_failed_checks() -> None:
    attribution = AttributionResult(
        run_id="run_test",
        visible_symptom_steps=[2],
        evidence_step_ids=[2],
    )

    assert "symptom_step_not_backed_by_failed_check" in _codes(
        attribution,
        [_event("evt_1", 1), _event("evt_2", 2)],
        _failed_verdict(check_steps=[1]),
    )


def test_verifier_evidence_references_must_exist_in_trace() -> None:
    verdict = _failed_verdict(check_steps=[2], evidence_steps=[3])
    verdict.evidence = [
        EvidenceItem(
            kind=EvidenceKind.FINAL_ANSWER,
            description="run-level evidence",
            step_ids=[4],
        )
    ]

    issues = validate_attribution_result(
        AttributionResult(run_id="run_test"),
        [_event("evt_1", 1)],
        verdict,
    )

    issue = next(issue for issue in issues if issue.code == "verifier_evidence_step_not_in_trace")
    assert "[2, 3, 4]" in issue.message
