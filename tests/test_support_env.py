"""Unit tests for SupportEnvironment (support_env.py).

SupportEnvironment is the facade the runner drives. Tests cover:
    - tool_specs() returns the task's available_tools subset
    - validate_call() accepts valid calls and rejects invalid ones
    - execute() dispatches correctly and returns error results (not crashes)
    - Constructor rejects tool names not present in the registry
"""

from __future__ import annotations

import pytest

from trace_harness.environment.registry import ToolRegistry, default_support_registry
from trace_harness.environment.state import Doc, DocStatus, Order, SupportState
from trace_harness.environment.support_env import SupportEnvironment
from trace_harness.environment.tools import ToolSideEffect
from trace_harness.models.base import ToolCall


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _order(name: str = "Alice", days: int = 10) -> Order:
    return Order(
        order_id="ORD-001",
        customer_name=name,
        plan="Pro",
        amount_usd=100.0,
        purchase_age_days=days,
    )


def _doc(doc_id: str = "policy", title: str = "refund policy") -> Doc:
    return Doc(doc_id=doc_id, title=title, content="details", status=DocStatus.CURRENT)


def _env(
    orders: list[Order] | None = None,
    docs: list[Doc] | None = None,
    available_tools: list[str] | None = None,
) -> SupportEnvironment:
    state = SupportState(orders=orders or [], docs=docs or [])
    return SupportEnvironment(state, available_tools=available_tools)


def _call(tool_name: str, **arguments) -> ToolCall:
    return ToolCall(tool_name=tool_name, arguments=arguments)


# ---------------------------------------------------------------------------
# tool_specs
# ---------------------------------------------------------------------------


def test_tool_specs_returns_all_four_tools_by_default() -> None:
    env = _env()
    names = {spec.name for spec in env.tool_specs()}
    assert names == {"search_docs", "get_order", "issue_refund", "create_ticket"}


def test_tool_specs_respects_available_tools_filter() -> None:
    env = _env(available_tools=["search_docs", "get_order"])
    names = {spec.name for spec in env.tool_specs()}
    assert names == {"search_docs", "get_order"}
    assert "issue_refund" not in names
    assert "create_ticket" not in names


def test_tool_specs_each_have_name_description_and_parameters() -> None:
    env = _env()
    for spec in env.tool_specs():
        assert spec.name
        assert spec.description
        assert isinstance(spec.parameters, dict)


# ---------------------------------------------------------------------------
# validate_call
# ---------------------------------------------------------------------------


def test_validate_call_valid_search_docs_returns_true() -> None:
    env = _env()
    ok, err = env.validate_call(_call("search_docs", query="refund"))
    assert ok is True
    assert err is None


def test_validate_call_unknown_tool_returns_false_with_message() -> None:
    env = _env()
    ok, err = env.validate_call(_call("fly_helicopter"))
    assert ok is False
    assert "fly_helicopter" in err


def test_validate_call_tool_not_in_available_tools_returns_false() -> None:
    env = _env(available_tools=["search_docs"])
    ok, err = env.validate_call(_call("issue_refund", customer_name="Alice", refund_type="cash", reason="r"))
    assert ok is False
    assert "issue_refund" in err


def test_validate_call_missing_required_argument_returns_false() -> None:
    env = _env()
    # search_docs requires "query"
    ok, err = env.validate_call(_call("search_docs"))
    assert ok is False
    assert err is not None


def test_validate_call_extra_argument_returns_false() -> None:
    env = _env()
    ok, err = env.validate_call(_call("search_docs", query="refund", typo_arg="oops"))
    assert ok is False


def test_validate_call_does_not_mutate_state() -> None:
    env = _env(orders=[_order()])
    refund_count_before = len(env.state.refunds)
    env.validate_call(_call("issue_refund", customer_name="Alice", refund_type="cash", reason="r"))
    assert len(env.state.refunds) == refund_count_before


# ---------------------------------------------------------------------------
# execute
# ---------------------------------------------------------------------------


def test_execute_search_docs_returns_ok_result() -> None:
    env = _env(docs=[_doc()])
    result = env.execute(_call("search_docs", query="refund"))
    assert result.status == "ok"
    assert result.tool_name == "search_docs"


def test_execute_get_order_found_returns_ok() -> None:
    env = _env(orders=[_order("Alice")])
    result = env.execute(_call("get_order", customer_name="Alice"))
    assert result.status == "ok"
    assert result.result["order"]["customer_name"] == "Alice"


def test_execute_get_order_not_found_returns_error_result_not_crash() -> None:
    env = _env()
    result = env.execute(_call("get_order", customer_name="Nobody"))
    assert result.status == "error"
    assert result.error is not None


def test_execute_invalid_call_returns_error_not_crash() -> None:
    env = _env()
    result = env.execute(_call("unknown_tool"))
    assert result.status == "error"


def test_execute_issue_refund_mutates_state() -> None:
    env = _env(orders=[_order("Alice")])
    env.execute(_call("issue_refund", customer_name="Alice", refund_type="cash", reason="r"))
    assert len(env.state.refunds) == 1


def test_execute_passes_step_id_to_handler() -> None:
    env = _env(orders=[_order("Alice")])
    env.execute(_call("issue_refund", customer_name="Alice", refund_type="cash", reason="r"), step_id=3)
    assert env.state.refunds[0].issued_at_step == 3


def test_execute_create_ticket_with_step_id() -> None:
    env = _env()
    env.execute(
        _call("create_ticket", customer_name="Alice", title="Q", notes="N"),
        step_id=6,
    )
    assert env.state.tickets[0].created_at_step == 6


# ---------------------------------------------------------------------------
# side_effect_for
# ---------------------------------------------------------------------------


def test_side_effect_for_known_tool_returns_correct_class() -> None:
    env = _env()
    assert env.side_effect_for("search_docs") == ToolSideEffect.READ_ONLY
    assert env.side_effect_for("get_order") == ToolSideEffect.READ_ONLY
    assert env.side_effect_for("issue_refund") == ToolSideEffect.EXTERNAL_IRREVERSIBLE
    assert env.side_effect_for("create_ticket") == ToolSideEffect.EXTERNAL_DURABLE


def test_side_effect_for_unknown_tool_returns_none() -> None:
    env = _env()
    assert env.side_effect_for("nonexistent") is None


# ---------------------------------------------------------------------------
# snapshot_state
# ---------------------------------------------------------------------------


def test_snapshot_state_returns_dict_with_expected_keys() -> None:
    env = _env(orders=[_order()])
    snap = env.snapshot_state()
    assert isinstance(snap, dict)
    assert "orders" in snap
    assert "refunds" in snap
    assert "tickets" in snap


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


def test_constructor_rejects_unknown_tool_names() -> None:
    state = SupportState()
    with pytest.raises(ValueError, match="not in the registry"):
        SupportEnvironment(state, available_tools=["nonexistent_tool"])


def test_constructor_accepts_subset_of_registry_tools() -> None:
    state = SupportState()
    env = SupportEnvironment(state, available_tools=["search_docs"])
    assert env.tool_specs()[0].name == "search_docs"


def test_fresh_environment_instances_do_not_share_state() -> None:
    env1 = _env(orders=[_order("Alice")])
    env2 = _env(orders=[_order("Alice")])
    env1.execute(_call("issue_refund", customer_name="Alice", refund_type="cash", reason="r"))
    assert len(env1.state.refunds) == 1
    assert len(env2.state.refunds) == 0
