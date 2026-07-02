"""Unit tests for individual tool handlers and ToolDefinition."""

from __future__ import annotations

import pytest

from trace_harness.environment.state import Doc, DocStatus, Order, RefundType, SupportState
from trace_harness.environment.tools import (
    CreateTicketArgs,
    GetOrderArgs,
    IssueRefundArgs,
    SearchDocsArgs,
    support_tool_definitions,
)


def _defs() -> dict:
    return {d.name: d for d in support_tool_definitions()}


@pytest.fixture
def state() -> SupportState:
    order = Order(
        order_id="ORD-0001",
        customer_name="Test Customer",
        plan="Pro Annual",
        amount_usd=100.0,
        purchase_age_days=10,
    )
    doc = Doc(
        doc_id="refund-policy",
        title="Refund Policy",
        status=DocStatus.CURRENT,
        content="Full cash refund within 30 days.",
    )
    return SupportState(orders=[order], docs=[doc])


# --- search_docs ---


def test_search_docs_returns_retrieval_side_channel(state):
    args = SearchDocsArgs(query="refund policy")
    result = _defs()["search_docs"].handler(state, args, step_id=None)
    assert result.status == "ok"
    assert result.retrieval is not None
    assert len(result.retrieval) > 0


def test_search_docs_invalid_status_filter_returns_error(state):
    args = SearchDocsArgs(query="refund", status_filter="bogus_status")
    result = _defs()["search_docs"].handler(state, args, step_id=None)
    assert result.status == "error"
    assert result.error is not None


def test_search_docs_ok_result_structure(state):
    args = SearchDocsArgs(query="refund")
    result = _defs()["search_docs"].handler(state, args, step_id=None)
    assert result.status == "ok"
    assert "query" in result.result
    assert "result_count" in result.result
    assert "results" in result.result


# --- get_order ---


def test_get_order_found(state):
    args = GetOrderArgs(customer_name="Test Customer")
    result = _defs()["get_order"].handler(state, args, step_id=None)
    assert result.status == "ok"
    assert "order" in result.result
    assert result.result["order"]["customer_name"] == "Test Customer"


def test_get_order_not_found(state):
    args = GetOrderArgs(customer_name="Unknown Person")
    result = _defs()["get_order"].handler(state, args, step_id=None)
    assert result.status == "error"
    assert result.error is not None
    assert "Unknown Person" in result.error


# --- issue_refund ---


def test_issue_refund_creates_refund_record(state):
    args = IssueRefundArgs(
        customer_name="Test Customer", refund_type=RefundType.CASH, reason="within window"
    )
    _defs()["issue_refund"].handler(state, args, step_id=None)
    assert len(state.refunds) == 1
    refund = state.refunds[0]
    assert refund.customer_name == "Test Customer"
    assert refund.refund_type == RefundType.CASH
    assert refund.amount_usd == 100.0


def test_issue_refund_step_id_provenance(state):
    args = IssueRefundArgs(
        customer_name="Test Customer", refund_type=RefundType.CASH, reason="test"
    )
    _defs()["issue_refund"].handler(state, args, step_id=5)
    assert state.refunds[0].issued_at_step == 5


def test_issue_refund_increments_seq(state):
    args = IssueRefundArgs(
        customer_name="Test Customer", refund_type=RefundType.CASH, reason="test"
    )
    _defs()["issue_refund"].handler(state, args, step_id=None)
    _defs()["issue_refund"].handler(state, args, step_id=None)
    assert state.refunds[0].refund_id == "REF-0001"
    assert state.refunds[1].refund_id == "REF-0002"


def test_issue_refund_unknown_customer_returns_error(state):
    args = IssueRefundArgs(customer_name="Nobody", refund_type=RefundType.CASH, reason="test")
    result = _defs()["issue_refund"].handler(state, args, step_id=None)
    assert result.status == "error"
    assert len(state.refunds) == 0  # no state mutation on error


def test_issue_refund_returns_ok_with_refund_id(state):
    args = IssueRefundArgs(
        customer_name="Test Customer", refund_type=RefundType.CASH, reason="test"
    )
    result = _defs()["issue_refund"].handler(state, args, step_id=None)
    assert result.status == "ok"
    assert result.result["refund_id"] == state.refunds[0].refund_id


# --- create_ticket ---


def test_create_ticket_creates_ticket_record(state):
    args = CreateTicketArgs(
        customer_name="Test Customer", title="Issue Report", notes="Details here."
    )
    _defs()["create_ticket"].handler(state, args, step_id=None)
    assert len(state.tickets) == 1
    ticket = state.tickets[0]
    assert ticket.customer_name == "Test Customer"
    assert ticket.title == "Issue Report"
    assert ticket.notes == "Details here."


def test_create_ticket_step_id_provenance(state):
    args = CreateTicketArgs(customer_name="Test Customer", title="T", notes="N")
    _defs()["create_ticket"].handler(state, args, step_id=3)
    assert state.tickets[0].created_at_step == 3


def test_create_ticket_increments_seq(state):
    args = CreateTicketArgs(customer_name="Test Customer", title="T", notes="N")
    _defs()["create_ticket"].handler(state, args, step_id=None)
    _defs()["create_ticket"].handler(state, args, step_id=None)
    assert state.tickets[0].ticket_id == "TICK-0001"
    assert state.tickets[1].ticket_id == "TICK-0002"


# --- ToolDefinition.spec ---


def test_tool_definition_spec_returns_json_schema():
    for defn in support_tool_definitions():
        spec = defn.spec()
        assert spec.name == defn.name
        assert isinstance(spec.description, str) and spec.description
        assert isinstance(spec.parameters, dict)
        assert "properties" in spec.parameters
