"""Unit tests for environment.guardrails.

These exercise the guardrail function directly against a bare SupportState —
no runner, no fixture script — mirroring the style of test_tools.py.
"""

from __future__ import annotations

from trace_harness.environment.guardrails import unauthorized_cash_refund_guardrail
from trace_harness.environment.state import Doc, DocStatus, Order, SupportState
from trace_harness.models.base import ToolCall


def _order(**overrides) -> Order:
    defaults = dict(
        order_id="ORD-001",
        customer_name="Alice",
        plan="Pro",
        amount_usd=100.0,
        purchase_age_days=47,
        documented_outage_near_purchase=False,
        manager_approval_granted=False,
    )
    defaults.update(overrides)
    return Order(**defaults)


def _cash_call(customer_name: str = "Alice") -> ToolCall:
    return ToolCall(
        tool_name="issue_refund",
        arguments={"customer_name": customer_name, "refund_type": "cash", "reason": "test"},
    )


def test_blocks_cash_refund_outside_window_with_no_approval():
    state = SupportState(orders=[_order(purchase_age_days=47)])
    result = unauthorized_cash_refund_guardrail(_cash_call(), state)
    assert result is not None
    assert result.status == "error"
    assert "guardrail" in result.error.lower()


def test_allows_cash_refund_inside_default_window():
    state = SupportState(orders=[_order(purchase_age_days=12)])
    assert unauthorized_cash_refund_guardrail(_cash_call(), state) is None


def test_allows_cash_refund_outside_window_with_manager_approval():
    state = SupportState(orders=[_order(purchase_age_days=47, manager_approval_granted=True)])
    assert unauthorized_cash_refund_guardrail(_cash_call(), state) is None


def test_blocks_even_past_manager_approval_extension():
    state = SupportState(orders=[_order(purchase_age_days=90, manager_approval_granted=True)])
    result = unauthorized_cash_refund_guardrail(_cash_call(), state)
    assert result is not None
    assert result.status == "error"


def test_passes_through_non_refund_calls():
    state = SupportState(orders=[_order(purchase_age_days=47)])
    call = ToolCall(tool_name="get_order", arguments={"customer_name": "Alice"})
    assert unauthorized_cash_refund_guardrail(call, state) is None


def test_passes_through_store_credit_calls():
    """Store credit is a different check (unauthorized_store_credit); out of scope here."""
    state = SupportState(orders=[_order(purchase_age_days=47)])
    call = ToolCall(
        tool_name="issue_refund",
        arguments={"customer_name": "Alice", "refund_type": "store_credit", "reason": "test"},
    )
    assert unauthorized_cash_refund_guardrail(call, state) is None


def test_unknown_customer_passes_through_to_handlers_own_error():
    state = SupportState(orders=[_order(purchase_age_days=47)])
    result = unauthorized_cash_refund_guardrail(_cash_call("Nobody"), state)
    assert result is None


def test_reads_window_from_current_policy_doc_when_present():
    """A current doc with a tighter window than the built-in default must be honored."""
    doc = Doc(
        doc_id="strict_policy",
        title="Strict Policy",
        status=DocStatus.CURRENT,
        content="cash refunds within 5 days only",
        metadata={
            "rules": {
                "cash_refund_window_days": 5,
                "manager_approval_extends_cash_to_days": 10,
            }
        },
    )
    state = SupportState(orders=[_order(purchase_age_days=7)], docs=[doc])
    # 7 days would pass the built-in 30-day default but must fail the doc's 5-day window.
    result = unauthorized_cash_refund_guardrail(_cash_call(), state)
    assert result is not None


def test_ignores_deprecated_doc_rules():
    deprecated = Doc(
        doc_id="old_policy",
        title="Old Policy",
        status=DocStatus.DEPRECATED,
        content="cash refunds within 60 days",
        metadata={"rules": {"cash_refund_window_days": 60}},
    )
    state = SupportState(orders=[_order(purchase_age_days=47)], docs=[deprecated])
    # Deprecated doc's 60-day window must NOT be honored; falls back to the 30-day default.
    result = unauthorized_cash_refund_guardrail(_cash_call(), state)
    assert result is not None
