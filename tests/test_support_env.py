"""Unit tests for SupportEnvironment."""

from __future__ import annotations

from pathlib import Path

import pytest

from trace_harness.environment.support_env import SupportEnvironment
from trace_harness.environment.tools import ToolSideEffect
from trace_harness.models.base import ToolCall
from trace_harness.tasks.loader import load_docs_for_task, load_task
from trace_harness.tasks.schemas import TaskSpec

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
FAILURE_TASK_PATH = FIXTURES_DIR / "tasks" / "refund_policy_failure.json"


@pytest.fixture(scope="module")
def _failure_task_and_docs():
    task = load_task(FAILURE_TASK_PATH)
    docs = load_docs_for_task(task, FAILURE_TASK_PATH)
    return task, docs


@pytest.fixture
def env(_failure_task_and_docs):
    task, docs = _failure_task_and_docs
    return SupportEnvironment.from_task(task, docs=docs)


# --- tool_specs ---


def test_tool_specs_returns_subset_from_task(env, _failure_task_and_docs):
    task, _ = _failure_task_and_docs
    specs = env.tool_specs()
    assert {s.name for s in specs} == set(task.available_tools)


def test_tool_spec_has_json_schema(env):
    for spec in env.tool_specs():
        assert isinstance(spec.name, str) and spec.name
        assert isinstance(spec.description, str) and spec.description
        assert isinstance(spec.parameters, dict)
        assert "properties" in spec.parameters


# --- validate_call ---


def test_validate_call_valid_tool_and_args(env):
    call = ToolCall(tool_name="search_docs", arguments={"query": "refund policy"})
    ok, error = env.validate_call(call)
    assert ok is True
    assert error is None


def test_validate_call_unknown_tool(env):
    call = ToolCall(tool_name="nonexistent_tool", arguments={})
    ok, error = env.validate_call(call)
    assert ok is False
    assert error is not None
    assert "nonexistent_tool" in error


def test_validate_call_missing_required_arg(env):
    call = ToolCall(tool_name="get_order", arguments={})  # customer_name is required
    ok, error = env.validate_call(call)
    assert ok is False
    assert error is not None


def test_validate_call_extra_arg_rejected(env):
    call = ToolCall(
        tool_name="search_docs",
        arguments={"query": "refund", "unexpected_key": "value"},
    )
    ok, error = env.validate_call(call)
    assert ok is False
    assert error is not None


def test_validate_call_no_side_effect(env):
    call = ToolCall(
        tool_name="issue_refund",
        arguments={"customer_name": "Casey Nguyen", "refund_type": "cash", "reason": "test"},
    )
    env.validate_call(call)
    assert env.state.refunds == []  # validation must not mutate state


# --- execute ---


def test_execute_valid_call_mutates_state(env):
    call = ToolCall(
        tool_name="issue_refund",
        arguments={"customer_name": "Casey Nguyen", "refund_type": "cash", "reason": "test"},
    )
    result = env.execute(call, step_id=1)
    assert result.status == "ok"
    assert len(env.state.refunds) == 1


def test_execute_invalid_call_returns_error_not_crash(env):
    call = ToolCall(tool_name="get_order", arguments={})  # missing customer_name
    result = env.execute(call, step_id=1)
    assert result.status == "error"
    assert result.error is not None


def test_execute_invalid_call_does_not_mutate_state(env):
    call = ToolCall(
        tool_name="issue_refund",
        arguments={"customer_name": "Casey Nguyen", "refund_type": "bad_type", "reason": "test"},
    )
    env.execute(call)
    assert env.state.refunds == []


def test_execute_with_step_id(env):
    call = ToolCall(
        tool_name="issue_refund",
        arguments={"customer_name": "Casey Nguyen", "refund_type": "cash", "reason": "test"},
    )
    env.execute(call, step_id=5)
    assert env.state.refunds[0].issued_at_step == 5


# --- side_effect_for ---


def test_side_effect_for_search_docs(env):
    assert env.side_effect_for("search_docs") == ToolSideEffect.READ_ONLY


def test_side_effect_for_get_order(env):
    assert env.side_effect_for("get_order") == ToolSideEffect.READ_ONLY


def test_side_effect_for_issue_refund(env):
    assert env.side_effect_for("issue_refund") == ToolSideEffect.EXTERNAL_IRREVERSIBLE


def test_side_effect_for_create_ticket(env):
    assert env.side_effect_for("create_ticket") == ToolSideEffect.EXTERNAL_DURABLE


def test_side_effect_for_unknown_tool(env):
    assert env.side_effect_for("nonexistent") is None


# --- snapshot_state ---


def test_snapshot_state_is_dict(env):
    snap = env.snapshot_state()
    assert isinstance(snap, dict)


def test_snapshot_state_initial_empty_side_effects(env):
    snap = env.snapshot_state()
    assert snap["refunds"] == []
    assert snap["tickets"] == []


def test_snapshot_state_reflects_mutations(env):
    call = ToolCall(
        tool_name="issue_refund",
        arguments={"customer_name": "Casey Nguyen", "refund_type": "cash", "reason": "test"},
    )
    env.execute(call, step_id=1)
    snap = env.snapshot_state()
    assert len(snap["refunds"]) == 1


# --- constructor / from_task ---


def test_from_task_raises_on_unknown_tool():
    task = TaskSpec(
        task_id="test-task",
        title="Test",
        description="Test",
        goal="Test",
        workflow_type="support.refund",
        initial_state={},
        available_tools=["nonexistent_tool"],
    )
    with pytest.raises(ValueError, match="not in the registry"):
        SupportEnvironment.from_task(task, docs=[])


def test_fresh_environment_is_clean(_failure_task_and_docs):
    task, docs = _failure_task_and_docs
    env1 = SupportEnvironment.from_task(task, docs=docs)
    env2 = SupportEnvironment.from_task(task, docs=docs)

    env1.execute(
        ToolCall(
            tool_name="issue_refund",
            arguments={"customer_name": "Casey Nguyen", "refund_type": "cash", "reason": "test"},
        ),
        step_id=1,
    )

    assert len(env1.state.refunds) == 1
    assert len(env2.state.refunds) == 0
