"""FailureBundleGenerator: card, repair package, and regression artifact."""

from __future__ import annotations

import pytest

from trace_harness.attribution.heuristic import HeuristicAttributor
from trace_harness.failure_bundles.generator import FailureBundleGenerator
from trace_harness.tasks.schemas import Severity
from trace_harness.verifiers.base import VerifierInput
from trace_harness.verifiers.registry import get_verifier


@pytest.fixture
def bundle_inputs(failure_run):
    verifier_result = get_verifier(failure_run.task.verifier_ids[0]).verify(
        VerifierInput.from_parts(
            task=failure_run.task,
            trace=failure_run.trace,
            final_state=failure_run.final_state,
            run_id=failure_run.run_id,
        )
    )
    attribution = HeuristicAttributor().attribute(
        failure_run.task, failure_run.trace, verifier_result
    )
    return failure_run, verifier_result, attribution


def _generate(run, verifier_result, attribution):
    return FailureBundleGenerator().generate(
        task=run.task,
        run_result=run.result,
        trace=run.trace,
        verifier_result=verifier_result,
        attribution=attribution,
        final_state=run.final_state,
        initial_state=run.initial_state,
        task_fixture_path="fixtures/tasks/refund_policy_failure.json",
    )


def test_failure_card_is_complete_and_evidence_backed(bundle_inputs):
    run, verifier_result, attribution = bundle_inputs
    card = _generate(run, verifier_result, attribution).failure_card

    assert card.run_id == run.run_id
    assert card.task_id == "refund_policy_failure"
    assert card.severity is Severity.CRITICAL
    assert "FAILED" in card.task_result
    assert "step 3" in card.root_cause
    assert len(card.visible_symptoms) == 3
    assert card.evidence, "a failure card without evidence is an opinion"
    assert card.causal_explanation == attribution.causal_explanation
    assert card.blast_radius.refund_total_usd == 432.0
    assert card.blast_radius.refund_count == 1
    assert card.blast_radius.ticket_count == 1
    assert card.blast_radius.customers_affected == ["Casey Nguyen"]
    assert "$432.00" in card.blast_radius.summary


def test_failure_card_contributing_failures_and_step_ids(bundle_inputs):
    run, verifier_result, attribution = bundle_inputs
    card = _generate(run, verifier_result, attribution).failure_card

    # Primary category is first; contributing categories follow.
    assert card.contributing_failures, "contributing_failures must not be empty"
    assert card.contributing_failures[0] == attribution.primary_failure_category.value
    contributing_values = [c.value for c in attribution.contributing_failure_categories]
    assert card.contributing_failures[1:] == contributing_values

    # step_ids are the union of all failed-check step_ids, sorted.
    expected_steps = sorted(
        {step for check in verifier_result.failed_checks for step in check.step_ids}
    )
    assert card.step_ids == expected_steps
    assert card.step_ids, "at least one relevant step must be identified"


def test_repair_package_controls_are_actionable(bundle_inputs):
    run, verifier_result, attribution = bundle_inputs
    package = _generate(run, verifier_result, attribution).repair_package

    names = [control.name for control in package.controls]
    assert names == [
        "deterministic_pre_call_refund_guardrail",
        "ticket_claim_grounding_check",
        "current_policy_source_precedence",
        "regression_test_ci_gate",
    ]
    failed_ids = {check.check_id for check in verifier_result.failed_checks}
    for control in package.controls:
        # Every field must carry real content — empty controls are noise.
        assert control.installation_point
        assert control.check
        assert control.behavior_on_failure
        assert control.why_it_prevents_recurrence
        assert control.risk_or_tradeoff
        assert control.priority in {"P0", "P1", "P2", "P3"}
        assert control.linked_verifier_checks
        assert set(control.linked_verifier_checks) <= failed_ids
    guardrail = package.controls[0]
    assert guardrail.priority == "P0"
    assert "SupportEnvironment.execute" in guardrail.installation_point

    # Every control must have a non-empty expected_impact describing the
    # observable engineering outcome, distinct from the causal explanation.
    for control in package.controls:
        assert control.expected_impact, f"control {control.name!r} is missing expected_impact"


def test_regression_artifact_pins_the_failure(bundle_inputs):
    run, verifier_result, attribution = bundle_inputs
    regression = _generate(run, verifier_result, attribution).regression_artifact

    assert regression.test_name == "regression_refund_policy_failure"
    assert regression.source_run_id == run.run_id
    assert set(regression.verifier_checks) == {
        "unauthorized_cash_refund",
        "deprecated_policy_treated_as_authoritative",
        "ticket_outage_claim_unsupported",
    }
    assert regression.severity is Severity.CRITICAL
    assert regression.blocks_release is True
    # Pinned world: docs and state as the failing run saw them.
    assert {d["doc_id"] for d in regression.pinned_docs} == {
        "refund_policy_v2",
        "refund_policy_v4",
        "export_incident_2025_01",
    }
    assert regression.initial_state["orders"][0]["purchase_age_days"] == 47
    assert regression.expected_behavior == run.task.expected_behavior
    assert regression.forbidden_actions == run.task.forbidden_actions
    # Replayable, with an anti-overblocking positive sibling.
    assert regression.replay_command == (
        "trace-harness run-pipeline fixtures/tasks/refund_policy_failure.json"
    )
    assert len(regression.positive_sibling_tests) == 1
    sibling = regression.positive_sibling_tests[0]
    assert sibling.task_fixture == "fixtures/tasks/refund_policy_valid_cash.json"


def test_bundle_refuses_passing_runs(valid_run):
    from trace_harness.attribution.schemas import AttributionResult

    verifier_result = get_verifier(valid_run.task.verifier_ids[0]).verify(
        VerifierInput.from_parts(
            task=valid_run.task,
            trace=valid_run.trace,
            final_state=valid_run.final_state,
            run_id=valid_run.run_id,
        )
    )
    assert verifier_result.passed
    with pytest.raises(ValueError, match="passing run"):
        FailureBundleGenerator().generate(
            task=valid_run.task,
            run_result=valid_run.result,
            trace=valid_run.trace,
            verifier_result=verifier_result,
            attribution=AttributionResult(run_id=valid_run.run_id),
            final_state=valid_run.final_state,
            initial_state=valid_run.initial_state,
        )


# --- review-hardening regressions -------------------------------------------


def test_final_answer_failure_gets_a_grounding_control_and_ids_are_deduped(failure_run):
    """Every blocking check class yields a control; repeated ids collapse."""
    from trace_harness.tasks.schemas import Severity
    from trace_harness.verifiers.base import FailedCheck, VerifierResult

    def check(check_id, step):
        return FailedCheck(
            check_id=check_id,
            message="m",
            expected="e",
            actual="a",
            step_ids=[step],
            severity=Severity.HIGH,
        )

    verdict = VerifierResult(
        verifier_id="refund_policy",
        run_id=failure_run.run_id,
        passed=False,
        failed_checks=[
            check("unauthorized_cash_refund", 3),
            check("unauthorized_cash_refund", 5),  # same class, second record
            check("final_answer_inconsistent_with_state", 7),
        ],
    )
    package = FailureBundleGenerator()._build_repair_package(
        failure_run.task, failure_run.result, verdict
    )
    names = [control.name for control in package.controls]
    assert "final_answer_state_grounding_check" in names
    guardrail = next(c for c in package.controls if c.name.endswith("refund_guardrail"))
    assert guardrail.linked_verifier_checks == ["unauthorized_cash_refund"]  # deduped
    assert package.metadata["generated_from_checks"] == [
        "unauthorized_cash_refund",
        "final_answer_inconsistent_with_state",
    ]
