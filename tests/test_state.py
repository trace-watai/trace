"""Unit tests for SupportState and supporting models."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from trace_harness.environment.state import Doc, DocStatus, Refund, RefundType, SupportState
from trace_harness.tasks.loader import load_docs_for_task, load_task
from trace_harness.tasks.schemas import TaskSpec

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
FAILURE_TASK_PATH = FIXTURES_DIR / "tasks" / "refund_policy_failure.json"


def _minimal_task(**overrides) -> TaskSpec:
    defaults = dict(
        task_id="test-task",
        title="Test Task",
        description="Test task for unit tests.",
        goal="Run test.",
        workflow_type="support.refund",
        initial_state={},
        available_tools=[],
    )
    defaults.update(overrides)
    return TaskSpec(**defaults)


@pytest.fixture(scope="module")
def failure_task_with_docs():
    task = load_task(FAILURE_TASK_PATH)
    docs = load_docs_for_task(task, FAILURE_TASK_PATH)
    return task, docs


# --- from_task ---


def test_from_task_builds_correct_orders(failure_task_with_docs):
    task, docs = failure_task_with_docs
    state = SupportState.from_task(task, docs=docs)
    assert len(state.orders) == 1
    order = state.orders[0]
    assert order.customer_name == "Casey Nguyen"
    assert order.purchase_age_days == 47
    assert order.amount_usd == 432.0
    assert order.manager_approval_granted is False


def test_from_task_loads_fixture_docs(failure_task_with_docs):
    task, docs = failure_task_with_docs
    state = SupportState.from_task(task, docs=docs)
    doc_ids = {d.doc_id for d in state.docs}
    assert "refund_policy_v4" in doc_ids
    assert "refund_policy_v2" in doc_ids
    assert "export_incident_2025_01" in doc_ids


def test_from_task_merges_inline_docs():
    task = _minimal_task(
        initial_state={
            "docs": [
                {
                    "doc_id": "inline-doc",
                    "title": "Inline Doc",
                    "status": "current",
                    "content": "Inline content.",
                }
            ]
        }
    )
    fixture_doc = Doc(
        doc_id="fixture-doc",
        title="Fixture Doc",
        status=DocStatus.CURRENT,
        content="Fixture content.",
    )
    state = SupportState.from_task(task, docs=[fixture_doc])
    doc_ids = [d.doc_id for d in state.docs]
    # Fixture docs come first; inline docs are appended after.
    assert "fixture-doc" in doc_ids
    assert "inline-doc" in doc_ids
    assert doc_ids.index("fixture-doc") < doc_ids.index("inline-doc")


# --- find_order ---


def test_find_order_case_insensitive(failure_task_with_docs):
    task, docs = failure_task_with_docs
    state = SupportState.from_task(task, docs=docs)
    assert state.find_order("casey nguyen") is not None
    assert state.find_order("CASEY NGUYEN") is not None
    found = state.find_order("casey nguyen")
    assert found is not None
    assert found.customer_name == "Casey Nguyen"


def test_find_order_unknown_returns_none(failure_task_with_docs):
    task, docs = failure_task_with_docs
    state = SupportState.from_task(task, docs=docs)
    assert state.find_order("Nobody Here") is None


# --- snapshot ---


def test_snapshot_is_json_safe(failure_task_with_docs):
    task, docs = failure_task_with_docs
    state = SupportState.from_task(task, docs=docs)
    snap = state.snapshot()
    assert isinstance(snap, dict)
    json.dumps(snap)  # must not raise


def test_snapshot_deep_copy(failure_task_with_docs):
    task, docs = failure_task_with_docs
    state = SupportState.from_task(task, docs=docs)
    state.refunds.append(
        Refund(
            refund_id="REF-0001",
            order_id="ORD-0001",
            customer_name="Casey Nguyen",
            refund_type=RefundType.CASH,
            amount_usd=432.0,
            reason="test",
            issued_at_step=1,
        )
    )
    snap = state.snapshot()
    assert len(snap["refunds"]) == 1
    # Mutating state after snapshot must not change the snapshot.
    state.refunds.clear()
    assert len(snap["refunds"]) == 1


# --- ID counter seeding ---


def test_id_counter_seeds_past_preexisting_refunds():
    task = _minimal_task(
        initial_state={
            "refunds": [
                {
                    "refund_id": "REF-0003",
                    "order_id": "ORD-001",
                    "customer_name": "Test",
                    "refund_type": "cash",
                    "amount_usd": 100.0,
                    "reason": "existing refund",
                }
            ]
        }
    )
    state = SupportState.from_task(task, docs=[])
    assert state.next_refund_seq == 4


def test_id_counter_seeds_past_preexisting_tickets():
    task = _minimal_task(
        initial_state={
            "tickets": [
                {
                    "ticket_id": "TICK-0007",
                    "customer_name": "Test",
                    "title": "Old Ticket",
                    "notes": "Some notes.",
                }
            ]
        }
    )
    state = SupportState.from_task(task, docs=[])
    assert state.next_ticket_seq == 8


# --- validation ---


def test_extra_forbid_on_initial_state():
    with pytest.raises(ValidationError):
        SupportState.model_validate({"ordes": []})  # typo: "ordes" not "orders"


def test_doc_status_values():
    assert DocStatus.CURRENT == "current"
    assert DocStatus.DEPRECATED == "deprecated"
    assert DocStatus.RESOLVED == "resolved"
