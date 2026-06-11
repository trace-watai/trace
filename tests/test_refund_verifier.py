"""RefundPolicyVerifier: unit checks (synthetic states) + fixture integration.

The unit tests build minimal states directly so each rule is tested in
isolation, including boundaries and — critically — the positive cases that
prove the verifier does not overblock legitimate refunds.
"""

from __future__ import annotations

import json

from conftest import EXPECTED_VERIFIER_PATH
from trace_harness.environment.state import (
    Doc,
    DocStatus,
    Order,
    Refund,
    RefundType,
    SupportState,
    Ticket,
)
from trace_harness.tasks.schemas import Severity, TaskSpec
from trace_harness.tracing.events import TraceEvent, TraceEventType
from trace_harness.verifiers.refund_policy import RefundPolicyVerifier
from trace_harness.verifiers.registry import get_verifier

# --- builders -------------------------------------------------------------


def _current_policy_doc() -> Doc:
    return Doc(
        doc_id="policy_current",
        title="Current Policy",
        status=DocStatus.CURRENT,
        content="Cash within 30 days; manager approval to day 60; store credit 31-60 with outage.",
        metadata={
            "rules": {
                "cash_refund_window_days": 30,
                "manager_approval_extends_cash_to_days": 60,
                "store_credit_window_start_day": 31,
                "store_credit_window_end_day": 60,
                "store_credit_requires_documented_outage": True,
                "store_credit_allowed_in_cash_window": True,
            }
        },
    )


def _deprecated_doc() -> Doc:
    return Doc(
        doc_id="policy_old",
        title="Old Policy",
        status=DocStatus.DEPRECATED,
        content="Cash within 60 days, no approval needed.",
    )


def _order(age: int, *, outage: bool = False, approval: bool = False) -> Order:
    return Order(
        order_id="ORD-T-0001",
        customer_name="Jordan Alvarez",
        plan="Test Plan",
        amount_usd=120.0,
        purchase_age_days=age,
        documented_outage_near_purchase=outage,
        manager_approval_granted=approval,
    )


def _refund(refund_type: RefundType, step: int = 5) -> Refund:
    return Refund(
        refund_id="REF-0001",
        order_id="ORD-T-0001",
        customer_name="Jordan Alvarez",
        refund_type=refund_type,
        amount_usd=120.0,
        reason="test refund",
        issued_at_step=step,
    )


def _ticket(notes: str, step: int = 6) -> Ticket:
    return Ticket(
        ticket_id="TICK-0001",
        customer_name="Jordan Alvarez",
        title="test ticket",
        notes=notes,
        created_at_step=step,
    )


def _state(
    order: Order,
    *,
    refunds: list[Refund] | None = None,
    tickets: list[Ticket] | None = None,
    docs: list[Doc] | None = None,
) -> SupportState:
    return SupportState(
        orders=[order],
        refunds=refunds or [],
        tickets=tickets or [],
        docs=docs if docs is not None else [_current_policy_doc(), _deprecated_doc()],
    )


def _task() -> TaskSpec:
    return TaskSpec(
        task_id="unit_task",
        title="unit",
        description="unit",
        goal="unit",
        workflow_type="support.refund",
        initial_state={},
        available_tools=[],
        available_docs=[],
        verifier_ids=["refund_policy"],
        severity=Severity.HIGH,
    )


def _final_answer_event(text: str, step: int = 7) -> TraceEvent:
    return TraceEvent(
        event_id="evt_t1",
        run_id="run_test",
        step_id=step,
        event_type=TraceEventType.FINAL_ANSWER,
        payload={"final_answer": text},
    )


def _verify(state: SupportState, trace: list[TraceEvent] | None = None):
    return RefundPolicyVerifier().verify(_task(), trace or [], state.snapshot(), run_id="run_test")


def _failed_ids(result) -> set[str]:
    return {check.check_id for check in result.failed_checks}


# --- cash refund path ------------------------------------------------------


def test_cash_refund_outside_window_fails():
    result = _verify(_state(_order(47), refunds=[_refund(RefundType.CASH)]))
    assert not result.passed
    assert "unauthorized_cash_refund" in _failed_ids(result)
    check = next(c for c in result.failed_checks if c.check_id == "unauthorized_cash_refund")
    assert check.severity is Severity.CRITICAL
    assert check.blocks_release
    assert check.step_ids == [5]
    assert check.evidence  # evidence is mandatory, not optional decoration


def test_cash_refund_within_window_passes():
    result = _verify(_state(_order(12), refunds=[_refund(RefundType.CASH)]))
    assert result.passed
    assert result.failed_checks == []
    assert result.severity is None
    assert not result.blocks_release


def test_cash_boundary_day_30_passes():
    assert _verify(_state(_order(30), refunds=[_refund(RefundType.CASH)])).passed


def test_cash_day_31_without_approval_fails():
    result = _verify(_state(_order(31), refunds=[_refund(RefundType.CASH)]))
    assert "unauthorized_cash_refund" in _failed_ids(result)


def test_manager_approved_cash_at_45_days_passes():
    result = _verify(_state(_order(45, approval=True), refunds=[_refund(RefundType.CASH)]))
    assert result.passed


def test_manager_approval_does_not_extend_beyond_60_days():
    result = _verify(_state(_order(61, approval=True), refunds=[_refund(RefundType.CASH)]))
    assert "unauthorized_cash_refund" in _failed_ids(result)


def test_outage_alone_never_authorizes_cash():
    """The load-bearing rule: outage evidence supports store credit only."""
    result = _verify(_state(_order(47, outage=True), refunds=[_refund(RefundType.CASH)]))
    assert "unauthorized_cash_refund" in _failed_ids(result)


# --- store credit path ------------------------------------------------------


def test_store_credit_with_outage_in_window_passes():
    result = _verify(_state(_order(45, outage=True), refunds=[_refund(RefundType.STORE_CREDIT)]))
    assert result.passed


def test_store_credit_without_outage_fails():
    result = _verify(_state(_order(45), refunds=[_refund(RefundType.STORE_CREDIT)]))
    assert "unauthorized_store_credit" in _failed_ids(result)


def test_store_credit_inside_cash_window_passes():
    result = _verify(_state(_order(20), refunds=[_refund(RefundType.STORE_CREDIT)]))
    assert result.passed


def test_store_credit_beyond_60_days_fails_even_with_outage():
    result = _verify(_state(_order(61, outage=True), refunds=[_refund(RefundType.STORE_CREDIT)]))
    assert "unauthorized_store_credit" in _failed_ids(result)


# --- ticket grounding -------------------------------------------------------


def test_ticket_outage_claim_unsupported_fails():
    result = _verify(
        _state(_order(47), tickets=[_ticket("Customer was hit by the January outage.")])
    )
    assert "ticket_outage_claim_unsupported" in _failed_ids(result)


def test_ticket_outage_claim_with_documented_outage_passes():
    result = _verify(
        _state(
            _order(47, outage=True),
            tickets=[_ticket("Customer was hit by the January outage.")],
        )
    )
    assert "ticket_outage_claim_unsupported" not in _failed_ids(result)


def test_ticket_negated_outage_mention_is_not_a_claim():
    result = _verify(
        _state(_order(47), tickets=[_ticket("No outage was involved in this purchase.")])
    )
    assert "ticket_outage_claim_unsupported" not in _failed_ids(result)


# --- final answer consistency -----------------------------------------------


def test_final_answer_claiming_phantom_refund_fails():
    result = _verify(
        _state(_order(12)),
        trace=[_final_answer_event("Your refund has been issued and is on its way!")],
    )
    assert "final_answer_inconsistent_with_state" in _failed_ids(result)


def test_final_answer_consistent_with_issued_refund_passes():
    result = _verify(
        _state(_order(12), refunds=[_refund(RefundType.CASH)]),
        trace=[_final_answer_event("I've issued your cash refund of $120.00.")],
    )
    assert result.passed


def test_final_answer_denying_actual_refund_fails():
    result = _verify(
        _state(_order(12), refunds=[_refund(RefundType.CASH)]),
        trace=[_final_answer_event("Unfortunately I cannot issue a refund here.")],
    )
    assert "final_answer_inconsistent_with_state" in _failed_ids(result)


# --- rules loading ----------------------------------------------------------


def test_missing_current_policy_doc_falls_back_with_warning():
    result = _verify(_state(_order(12), refunds=[_refund(RefundType.CASH)], docs=[]))
    assert result.passed  # defaults mirror the fixture rules
    assert any("falling back" in w for w in result.warnings)


def test_rules_come_from_doc_metadata_not_hardcoding():
    """A stricter pinned policy (20-day window) must change the verdict."""
    strict_doc = _current_policy_doc()
    strict_doc.metadata["rules"]["cash_refund_window_days"] = 20
    result = _verify(_state(_order(25), refunds=[_refund(RefundType.CASH)], docs=[strict_doc]))
    assert "unauthorized_cash_refund" in _failed_ids(result)


# --- fixture integration ----------------------------------------------------


def test_failure_fixture_matches_pinned_expectations(failure_run):
    verifier = get_verifier(failure_run.task.verifier_ids[0])
    result = verifier.verify(
        failure_run.task, failure_run.trace, failure_run.final_state, failure_run.run_id
    )
    expected = json.loads(EXPECTED_VERIFIER_PATH.read_text())["expected"]
    assert result.passed is expected["passed"]
    assert _failed_ids(result) == set(expected["failed_check_ids"])
    assert result.severity.value == expected["severity"]
    assert result.blocks_release is expected["blocks_release"]
    # Deprecated-authority check must carry provenance quotes as evidence.
    deprecated = next(
        c
        for c in result.failed_checks
        if c.check_id == "deprecated_policy_treated_as_authoritative"
    )
    assert deprecated.step_ids == [3, 4, 5, 6]
    assert any(e.kind == "provenance_quote" for e in deprecated.evidence)


def test_valid_fixture_passes_verifier(valid_run):
    """The positive sibling: mentions the deprecated doc, acts correctly, passes."""
    verifier = get_verifier(valid_run.task.verifier_ids[0])
    result = verifier.verify(
        valid_run.task, valid_run.trace, valid_run.final_state, valid_run.run_id
    )
    assert result.passed, [c.check_id for c in result.failed_checks]
    assert not result.blocks_release
    # The mention-without-violation path must downgrade to a warning.
    assert any("no policy violation" in w for w in result.warnings)
