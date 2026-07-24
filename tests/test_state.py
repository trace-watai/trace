"""Unit tests for SupportState (state.py).

Covers: from_task() doc merging, find_order() case-insensitivity, snapshot()
shape, sequence-counter seeding, and the doc_ranking_override field.

All tests construct state directly or via a minimal TaskSpec — no full
fixture runs needed here. Fixture-backed tests verify against real scenario data.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from trace_harness.environment.state import Doc, DocStatus, Order, Refund, RefundType, SupportState, Ticket
from trace_harness.tasks.loader import load_docs_for_task, load_task
from trace_harness.tasks.schemas import TaskSpec

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
FAILURE_TASK_PATH = FIXTURES_DIR / "tasks" / "refund_policy_failure.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _task(**initial_state_overrides) -> TaskSpec:
    """Minimal TaskSpec with an overrideable initial_state."""
    initial_state = {}
    initial_state.update(initial_state_overrides)
    return TaskSpec(
        task_id="unit_task",
        title="unit",
        description="unit",
        goal="unit",
        workflow_type="support.refund",
        initial_state=initial_state,
        available_tools=["get_order"],
    )


def _doc(doc_id: str, status: DocStatus = DocStatus.CURRENT) -> Doc:
    return Doc(doc_id=doc_id, title=f"Title {doc_id}", content="content", status=status)


def _order(name: str = "Alice") -> Order:
    return Order(
        order_id="ORD-001",
        customer_name=name,
        plan="Pro",
        amount_usd=100.0,
        purchase_age_days=10,
    )


@pytest.fixture(scope="module")
def failure_task_with_docs():
    task = load_task(FAILURE_TASK_PATH)
    docs = load_docs_for_task(task, FAILURE_TASK_PATH)
    return task, docs


# ---------------------------------------------------------------------------
# from_task: doc merging
# ---------------------------------------------------------------------------


def test_from_task_with_no_docs_produces_empty_doc_list() -> None:
    state = SupportState.from_task(_task())
    assert state.docs == []


def test_from_task_accepts_fixture_docs_arg() -> None:
    docs = [_doc("policy_v4")]
    state = SupportState.from_task(_task(), docs=docs)
    assert len(state.docs) == 1
    assert state.docs[0].doc_id == "policy_v4"


def test_from_task_merges_fixture_docs_before_inline_docs() -> None:
    fixture_doc = _doc("from_fixture")
    inline_doc_dict = {
        "doc_id": "inline_doc",
        "title": "Inline",
        "content": "inline content",
        "status": "current",
    }
    state = SupportState.from_task(
        _task(docs=[inline_doc_dict]),
        docs=[fixture_doc],
    )
    assert len(state.docs) == 2
    assert state.docs[0].doc_id == "from_fixture"   # fixture docs come first
    assert state.docs[1].doc_id == "inline_doc"


def test_from_task_inline_docs_only_when_no_fixture_arg() -> None:
    inline_doc_dict = {
        "doc_id": "inline_only",
        "title": "Inline",
        "content": "content",
        "status": "deprecated",
    }
    state = SupportState.from_task(_task(docs=[inline_doc_dict]))
    assert len(state.docs) == 1
    assert state.docs[0].doc_id == "inline_only"
    assert state.docs[0].status == DocStatus.DEPRECATED


def test_from_task_loads_orders_from_initial_state() -> None:
    order_dict = {
        "order_id": "ORD-001",
        "customer_name": "Casey",
        "plan": "Pro Annual",
        "amount_usd": 432.0,
        "purchase_age_days": 47,
    }
    state = SupportState.from_task(_task(orders=[order_dict]))
    assert len(state.orders) == 1
    assert state.orders[0].customer_name == "Casey"


def test_from_task_builds_correct_orders_from_real_fixture(failure_task_with_docs) -> None:
    task, docs = failure_task_with_docs
    state = SupportState.from_task(task, docs=docs)
    assert len(state.orders) == 1
    order = state.orders[0]
    assert order.customer_name == "Casey Nguyen"
    assert order.purchase_age_days == 47
    assert order.amount_usd == 432.0
    assert order.manager_approval_granted is False


def test_from_task_loads_real_fixture_docs(failure_task_with_docs) -> None:
    task, docs = failure_task_with_docs
    state = SupportState.from_task(task, docs=docs)
    doc_ids = {d.doc_id for d in state.docs}
    assert "refund_policy_v4" in doc_ids
    assert "refund_policy_v2" in doc_ids
    assert "export_incident_2025_01" in doc_ids


# ---------------------------------------------------------------------------
# from_task: sequence counter seeding
# ---------------------------------------------------------------------------


def test_from_task_seeds_refund_seq_past_existing_ids() -> None:
    refund_dict = {
        "refund_id": "REF-0003",
        "order_id": "ORD-001",
        "customer_name": "Alice",
        "refund_type": "cash",
        "amount_usd": 100.0,
        "reason": "test",
    }
    state = SupportState.from_task(_task(refunds=[refund_dict]))
    # Seq must start at 4 so a new refund won't collide with REF-0003.
    assert state.next_refund_seq == 4


def test_from_task_seeds_ticket_seq_past_existing_ids() -> None:
    ticket_dict = {
        "ticket_id": "TICK-0007",
        "customer_name": "Alice",
        "title": "Old ticket",
        "notes": "historical",
    }
    state = SupportState.from_task(_task(tickets=[ticket_dict]))
    assert state.next_ticket_seq == 8


def test_from_task_explicit_seq_in_payload_is_respected() -> None:
    # If initial_state sets next_refund_seq explicitly, from_task uses that.
    state = SupportState.from_task(_task(next_refund_seq=99))
    assert state.next_refund_seq == 99


# ---------------------------------------------------------------------------
# from_task: doc_ranking_override loaded from initial_state
# ---------------------------------------------------------------------------


def test_from_task_loads_doc_ranking_override() -> None:
    state = SupportState.from_task(
        _task(doc_ranking_override=["policy_v2", "policy_v4"])
    )
    assert state.doc_ranking_override == ["policy_v2", "policy_v4"]


def test_from_task_ranking_override_defaults_to_none() -> None:
    state = SupportState.from_task(_task())
    assert state.doc_ranking_override is None


# ---------------------------------------------------------------------------
# find_order
# ---------------------------------------------------------------------------


def test_find_order_returns_matching_order() -> None:
    state = SupportState(orders=[_order("Alice")])
    found = state.find_order("Alice")
    assert found is not None
    assert found.customer_name == "Alice"


def test_find_order_is_case_insensitive() -> None:
    state = SupportState(orders=[_order("Casey Nguyen")])
    assert state.find_order("casey nguyen") is not None
    assert state.find_order("CASEY NGUYEN") is not None
    assert state.find_order("Casey Nguyen") is not None


def test_find_order_strips_whitespace() -> None:
    state = SupportState(orders=[_order("Alice")])
    assert state.find_order("  Alice  ") is not None


def test_find_order_returns_none_for_unknown_customer() -> None:
    state = SupportState(orders=[_order("Alice")])
    assert state.find_order("Bob") is None


def test_find_order_returns_none_on_empty_orders() -> None:
    state = SupportState()
    assert state.find_order("Alice") is None


def test_find_order_returns_first_match() -> None:
    o1 = Order(order_id="ORD-001", customer_name="Alice", plan="A", amount_usd=10, purchase_age_days=1)
    o2 = Order(order_id="ORD-002", customer_name="Alice", plan="B", amount_usd=20, purchase_age_days=2)
    state = SupportState(orders=[o1, o2])
    assert state.find_order("Alice").order_id == "ORD-001"


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------


def test_snapshot_returns_dict() -> None:
    state = SupportState(orders=[_order()])
    snap = state.snapshot()
    assert isinstance(snap, dict)


def test_snapshot_contains_expected_top_level_keys() -> None:
    state = SupportState()
    snap = state.snapshot()
    for key in ("schema_version", "orders", "refunds", "tickets", "escalations", "docs"):
        assert key in snap, f"snapshot missing key '{key}'"


def test_snapshot_is_json_safe() -> None:
    """All values must be JSON-serialisable (no Pydantic models, no datetimes)."""
    state = SupportState(orders=[_order()])
    snap = state.snapshot()
    json.dumps(snap)  # raises if not serialisable


def test_snapshot_does_not_share_references_with_state() -> None:
    state = SupportState(orders=[_order()])
    snap = state.snapshot()
    snap["orders"].clear()
    assert len(state.orders) == 1  # original unchanged


# ---------------------------------------------------------------------------
# DocStatus enum values
# ---------------------------------------------------------------------------


def test_doc_status_string_values() -> None:
    assert DocStatus.CURRENT == "current"
    assert DocStatus.DEPRECATED == "deprecated"
    assert DocStatus.RESOLVED == "resolved"


# ---------------------------------------------------------------------------
# extra="forbid" at load time
# ---------------------------------------------------------------------------


def test_unknown_field_in_initial_state_raises_at_load_time() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        SupportState(unknown_field="oops")
