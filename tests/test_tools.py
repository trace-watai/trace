"""Unit tests for the five support-environment tool handlers.

Handlers are reached through ToolDefinition objects fetched from the default
registry — this keeps tests at the public-API boundary while exercising each
handler in isolation with a minimal in-memory SupportState.

Side-effect contract under test:
    search_docs   → read_only:             no state mutation
    get_order     → read_only:             no state mutation
    issue_refund  → external_irreversible: appends Refund, increments seq
    create_ticket → external_durable:      appends Ticket, increments seq
    escalate_case → external_durable:      appends Escalation, increments seq
"""

from __future__ import annotations

import pytest

from trace_harness.environment.registry import default_support_registry
from trace_harness.environment.state import Doc, DocStatus, Order, RefundType, SupportState
from trace_harness.environment.tools import (
    CreateTicketArgs,
    EscalateCaseArgs,
    GetOrderArgs,
    IssueRefundArgs,
    SearchDocsArgs,
    ToolSideEffect,
)
from trace_harness.environment.retrieval import RetrievedChunk


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REGISTRY = default_support_registry()


def _handler(tool_name: str):
    defn = _REGISTRY.get(tool_name)
    assert defn is not None, f"tool '{tool_name}' not in default registry"
    return defn.handler


def _order(name: str = "Alice", days: int = 10, amount: float = 100.0) -> Order:
    return Order(
        order_id="ORD-001",
        customer_name=name,
        plan="Pro",
        amount_usd=amount,
        purchase_age_days=days,
    )


def _doc(doc_id: str = "policy", title: str = "refund policy") -> Doc:
    return Doc(doc_id=doc_id, title=title, content="full text", status=DocStatus.CURRENT)


def _state(**kwargs) -> SupportState:
    return SupportState(**kwargs)


# ---------------------------------------------------------------------------
# search_docs handler
# ---------------------------------------------------------------------------


def test_search_docs_returns_ok_status() -> None:
    state = _state(docs=[_doc()])
    result = _handler("search_docs")(state, SearchDocsArgs(query="refund"), None)
    assert result.status == "ok"
    assert result.tool_name == "search_docs"


def test_search_docs_populates_retrieval_side_channel() -> None:
    state = _state(docs=[_doc()])
    result = _handler("search_docs")(state, SearchDocsArgs(query="refund"), None)
    assert result.retrieval is not None
    assert len(result.retrieval) == 1
    assert isinstance(result.retrieval[0], RetrievedChunk)


def test_search_docs_result_dict_mirrors_retrieval() -> None:
    state = _state(docs=[_doc()])
    result = _handler("search_docs")(state, SearchDocsArgs(query="refund"), None)
    assert result.result["result_count"] == len(result.retrieval)
    assert result.result["query"] == "refund"


def test_search_docs_no_match_returns_ok_with_empty_results() -> None:
    state = _state(docs=[_doc(title="shipping info")])
    result = _handler("search_docs")(state, SearchDocsArgs(query="refund"), None)
    assert result.status == "ok"
    assert result.result["result_count"] == 0
    assert result.retrieval == []


def test_search_docs_invalid_status_filter_returns_error() -> None:
    state = _state(docs=[_doc()])
    result = _handler("search_docs")(
        state, SearchDocsArgs(query="refund", status_filter="active"), None
    )
    assert result.status == "error"
    assert "status_filter" in result.error


def test_search_docs_valid_status_filter_accepted() -> None:
    state = _state(docs=[_doc()])
    result = _handler("search_docs")(
        state, SearchDocsArgs(query="refund", status_filter="current"), None
    )
    assert result.status == "ok"


def test_search_docs_does_not_mutate_state() -> None:
    state = _state(docs=[_doc()])
    original_doc_count = len(state.docs)
    _handler("search_docs")(state, SearchDocsArgs(query="refund"), None)
    assert len(state.docs) == original_doc_count


def test_search_docs_side_effect_is_read_only() -> None:
    defn = _REGISTRY.get("search_docs")
    assert defn.side_effect == ToolSideEffect.READ_ONLY


# ---------------------------------------------------------------------------
# get_order handler
# ---------------------------------------------------------------------------


def test_get_order_found_returns_ok_with_order_data() -> None:
    state = _state(orders=[_order("Bob")])
    result = _handler("get_order")(state, GetOrderArgs(customer_name="Bob"), None)
    assert result.status == "ok"
    assert result.result["order"]["customer_name"] == "Bob"


def test_get_order_not_found_returns_error() -> None:
    state = _state(orders=[_order("Alice")])
    result = _handler("get_order")(state, GetOrderArgs(customer_name="Nobody"), None)
    assert result.status == "error"
    assert "Nobody" in result.error


def test_get_order_does_not_mutate_state() -> None:
    state = _state(orders=[_order("Alice")])
    _handler("get_order")(state, GetOrderArgs(customer_name="Alice"), None)
    assert len(state.orders) == 1


def test_get_order_side_effect_is_read_only() -> None:
    defn = _REGISTRY.get("get_order")
    assert defn.side_effect == ToolSideEffect.READ_ONLY


# ---------------------------------------------------------------------------
# issue_refund handler
# ---------------------------------------------------------------------------


def test_issue_refund_creates_refund_in_state() -> None:
    state = _state(orders=[_order("Casey", amount=432.0)])
    _handler("issue_refund")(
        state,
        IssueRefundArgs(customer_name="Casey", refund_type=RefundType.CASH, reason="requested"),
        None,
    )
    assert len(state.refunds) == 1
    assert state.refunds[0].customer_name == "Casey"
    assert state.refunds[0].amount_usd == 432.0
    assert state.refunds[0].refund_type == RefundType.CASH


def test_issue_refund_increments_seq_counter() -> None:
    state = _state(orders=[_order("Casey")])
    seq_before = state.next_refund_seq
    _handler("issue_refund")(
        state,
        IssueRefundArgs(customer_name="Casey", refund_type=RefundType.CASH, reason="r"),
        None,
    )
    assert state.next_refund_seq == seq_before + 1


def test_issue_refund_id_is_deterministic() -> None:
    state = _state(orders=[_order("Casey")])
    state.next_refund_seq = 1
    result = _handler("issue_refund")(
        state,
        IssueRefundArgs(customer_name="Casey", refund_type=RefundType.CASH, reason="r"),
        None,
    )
    assert result.result["refund_id"] == "REF-0001"


def test_issue_refund_records_step_id_provenance() -> None:
    state = _state(orders=[_order("Casey")])
    _handler("issue_refund")(
        state,
        IssueRefundArgs(customer_name="Casey", refund_type=RefundType.CASH, reason="r"),
        step_id=5,
    )
    assert state.refunds[0].issued_at_step == 5


def test_issue_refund_returns_ok_with_refund_id() -> None:
    state = _state(orders=[_order("Casey")])
    result = _handler("issue_refund")(
        state,
        IssueRefundArgs(customer_name="Casey", refund_type=RefundType.CASH, reason="r"),
        None,
    )
    assert result.status == "ok"
    assert result.result["refund_id"].startswith("REF-")
    assert result.result["status"] == "issued"


def test_issue_refund_unknown_customer_returns_error_without_mutating_state() -> None:
    state = _state(orders=[_order("Alice")])
    result = _handler("issue_refund")(
        state,
        IssueRefundArgs(customer_name="Nobody", refund_type=RefundType.CASH, reason="r"),
        None,
    )
    assert result.status == "error"
    assert len(state.refunds) == 0


def test_issue_refund_side_effect_is_irreversible() -> None:
    defn = _REGISTRY.get("issue_refund")
    assert defn.side_effect == ToolSideEffect.EXTERNAL_IRREVERSIBLE


# ---------------------------------------------------------------------------
# create_ticket handler
# ---------------------------------------------------------------------------


def test_create_ticket_appends_ticket_to_state() -> None:
    state = _state()
    _handler("create_ticket")(
        state,
        CreateTicketArgs(customer_name="Dana", title="Refund question", notes="Needs follow-up"),
        None,
    )
    assert len(state.tickets) == 1
    assert state.tickets[0].customer_name == "Dana"
    assert state.tickets[0].notes == "Needs follow-up"


def test_create_ticket_increments_seq_counter() -> None:
    state = _state()
    seq_before = state.next_ticket_seq
    _handler("create_ticket")(
        state,
        CreateTicketArgs(customer_name="Dana", title="T", notes="N"),
        None,
    )
    assert state.next_ticket_seq == seq_before + 1


def test_create_ticket_id_is_deterministic() -> None:
    state = _state()
    state.next_ticket_seq = 1
    result = _handler("create_ticket")(
        state,
        CreateTicketArgs(customer_name="Dana", title="T", notes="N"),
        None,
    )
    assert result.result["ticket_id"] == "TICK-0001"


def test_create_ticket_records_step_id_provenance() -> None:
    state = _state()
    _handler("create_ticket")(
        state,
        CreateTicketArgs(customer_name="Dana", title="T", notes="N"),
        step_id=6,
    )
    assert state.tickets[0].created_at_step == 6


def test_create_ticket_returns_ok_with_ticket_id() -> None:
    state = _state()
    result = _handler("create_ticket")(
        state,
        CreateTicketArgs(customer_name="Dana", title="T", notes="N"),
        None,
    )
    assert result.status == "ok"
    assert result.result["ticket_id"].startswith("TICK-")
    assert result.result["status"] == "created"


def test_create_ticket_side_effect_is_durable() -> None:
    defn = _REGISTRY.get("create_ticket")
    assert defn.side_effect == ToolSideEffect.EXTERNAL_DURABLE


# ---------------------------------------------------------------------------
# escalate_case handler
# ---------------------------------------------------------------------------


def test_escalate_case_appends_escalation_to_state() -> None:
    state = _state()
    _handler("escalate_case")(
        state,
        EscalateCaseArgs(customer_name="Dana", reason="Customer requested a supervisor"),
        None,
    )
    assert len(state.escalations) == 1
    assert state.escalations[0].customer_name == "Dana"
    assert state.escalations[0].reason == "Customer requested a supervisor"


def test_escalate_case_increments_seq_counter() -> None:
    state = _state()
    seq_before = state.next_escalation_seq
    _handler("escalate_case")(
        state,
        EscalateCaseArgs(customer_name="Dana", reason="r"),
        None,
    )
    assert state.next_escalation_seq == seq_before + 1


def test_escalate_case_id_is_deterministic() -> None:
    state = _state()
    state.next_escalation_seq = 1
    result = _handler("escalate_case")(
        state,
        EscalateCaseArgs(customer_name="Dana", reason="r"),
        None,
    )
    assert result.result["escalation_id"] == "ESC-0001"


def test_escalate_case_records_step_id_provenance() -> None:
    state = _state()
    _handler("escalate_case")(
        state,
        EscalateCaseArgs(customer_name="Dana", reason="r"),
        step_id=4,
    )
    assert state.escalations[0].created_at_step == 4


def test_escalate_case_returns_ok_with_escalation_id() -> None:
    state = _state()
    result = _handler("escalate_case")(
        state,
        EscalateCaseArgs(customer_name="Dana", reason="r"),
        None,
    )
    assert result.status == "ok"
    assert result.result["escalation_id"].startswith("ESC-")
    assert result.result["status"] == "escalated"


def test_escalate_case_side_effect_is_durable() -> None:
    defn = _REGISTRY.get("escalate_case")
    assert defn.side_effect == ToolSideEffect.EXTERNAL_DURABLE


# ---------------------------------------------------------------------------
# ToolDefinition.spec
# ---------------------------------------------------------------------------


def test_tool_definition_spec_has_name_description_and_parameters() -> None:
    for tool_name in (
        "search_docs",
        "get_order",
        "issue_refund",
        "create_ticket",
        "escalate_case",
    ):
        defn = _REGISTRY.get(tool_name)
        spec = defn.spec()
        assert spec.name == defn.name
        assert isinstance(spec.description, str) and spec.description
        assert isinstance(spec.parameters, dict)
        assert "properties" in spec.parameters
