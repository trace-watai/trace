"""Controls as data (TRA-91): registry, install/uninstall, selection, parity, CLI.

Unit tests build a bare SupportEnvironment the way test_guardrails.py builds a
bare SupportState; the CLI tests reuse the control-demo fixture the way
test_replay_control_flip.py does, so the registry path is proven equivalent to
the direct-import path it replaces.
"""

from __future__ import annotations

import pytest

from conftest import FIXTURES_DIR
from trace_harness.cli import main
from trace_harness.environment.controls import (
    GUARDRAIL_REGISTRY,
    MATERIALIZABLE_REPAIR_CONTROLS,
    REFUND_WINDOW_CONTROL_ID,
    ControlInstance,
    RuleRef,
    UnknownGuardrailError,
    guardrail_ref_for_repair_control,
    reference_controls,
    resolve_guardrail,
    select_controls,
)
from trace_harness.environment.guardrails import unauthorized_cash_refund_guardrail
from trace_harness.environment.state import Order, SupportState
from trace_harness.environment.support_env import SupportEnvironment
from trace_harness.failure_bundles import generator as bundle_generator
from trace_harness.models.base import ToolCall
from trace_harness.tracing import artifact_store as names

CONTROL_DEMO_TASK_PATH = FIXTURES_DIR / "tasks" / "refund_policy_control_demo.json"


def _env_with_late_order() -> SupportEnvironment:
    order = Order(
        order_id="ORD-001",
        customer_name="Alice",
        plan="Pro",
        amount_usd=100.0,
        purchase_age_days=47,
        documented_outage_near_purchase=False,
        manager_approval_granted=False,
    )
    return SupportEnvironment(SupportState(orders=[order]))


def _cash_call() -> ToolCall:
    return ToolCall(
        tool_name="issue_refund",
        arguments={"customer_name": "Alice", "refund_type": "cash", "reason": "test"},
    )


def _instance(
    control_id: str = "ctl_test", guardrail_ref: str = "unauthorized_cash_refund_guardrail"
):
    return ControlInstance(
        control_id=control_id,
        guardrail_ref=guardrail_ref,
        rule_ref=RuleRef(source="current_policy_doc", rules=["cash_refund_window_days"]),
    )


# --- registry ---


def test_registry_resolves_shipped_guardrail() -> None:
    assert (
        resolve_guardrail("unauthorized_cash_refund_guardrail")
        is unauthorized_cash_refund_guardrail
    )
    assert "unauthorized_cash_refund_guardrail" in GUARDRAIL_REGISTRY


def test_unknown_guardrail_ref_is_a_model_value_but_fails_at_install() -> None:
    instance = _instance(guardrail_ref="does_not_exist")  # the model itself accepts it
    env = _env_with_late_order()
    with pytest.raises(UnknownGuardrailError, match="does_not_exist"):
        env.install_control(instance)
    assert env.installed_controls == []
    # and dispatch is untouched: no hook was half-registered
    assert env.execute(_cash_call()).status == "ok"


# --- install / uninstall ---


def test_install_makes_control_visible_and_blocks() -> None:
    env = _env_with_late_order()
    env.install_control(_instance())
    assert [c.control_id for c in env.installed_controls] == ["ctl_test"]
    result = env.execute(_cash_call())
    assert result.status == "error"
    assert result.error and result.error.startswith("blocked by refund policy guardrail")
    assert env.state.refunds == []


def test_uninstall_removes_hook_and_call_passes_through() -> None:
    env = _env_with_late_order()
    env.install_control(_instance())
    env.uninstall_control("ctl_test")
    assert env.installed_controls == []
    assert env.execute(_cash_call()).status == "ok"
    assert len(env.state.refunds) == 1


def test_duplicate_control_id_rejected_and_unknown_uninstall_rejected() -> None:
    env = _env_with_late_order()
    env.install_control(_instance())
    with pytest.raises(ValueError, match="already installed"):
        env.install_control(_instance())
    with pytest.raises(ValueError, match="not installed"):
        env.uninstall_control("never_installed")


def test_raw_hooks_are_not_reported_as_controls() -> None:
    env = _env_with_late_order()
    env.register_pre_execute_hook(lambda call, state: None)
    assert env.installed_controls == []


# --- parity with the direct-import path ---


def test_reference_control_reproduces_guardrail_exactly() -> None:
    env = _env_with_late_order()
    direct = unauthorized_cash_refund_guardrail(_cash_call(), env.state)
    env.install_control(reference_controls()[0])
    via_control = env.execute(_cash_call())
    assert direct is not None
    assert via_control.model_dump() == direct.model_dump()


# --- selection ---


def test_select_controls_default_all_and_by_id() -> None:
    assert [c.control_id for c in select_controls(None)] == [REFUND_WINDOW_CONTROL_ID]
    assert [c.control_id for c in select_controls([REFUND_WINDOW_CONTROL_ID])] == [
        REFUND_WINDOW_CONTROL_ID
    ]
    with pytest.raises(ValueError, match="unknown control id"):
        select_controls(["ctl_nope"])


# --- prescribed-control correspondence ---


def test_every_prescribed_repair_control_has_a_materializability_entry() -> None:
    builders = bundle_generator._CONTROL_BUILDERS
    prescribed = {builder(["x"]).name for builder in builders.values()}
    prescribed.add("regression_test_ci_gate")  # appended by the generator, not a builder
    missing = prescribed - set(MATERIALIZABLE_REPAIR_CONTROLS)
    assert not missing, f"repair controls without a materializability entry: {sorted(missing)}"
    assert guardrail_ref_for_repair_control("deterministic_pre_call_refund_guardrail") == (
        "unauthorized_cash_refund_guardrail"
    )
    assert guardrail_ref_for_repair_control("current_policy_source_precedence") is None
    assert guardrail_ref_for_repair_control("not_a_control") is None


# --- CLI: registry path, per-control selection, usage errors ---


def _bundle_artifact(tmp_path):
    runs_dir = tmp_path / "runs_bundle"
    assert main(["--runs-dir", str(runs_dir), "run-pipeline", str(CONTROL_DEMO_TASK_PATH)]) == 0
    (run_dir,) = [p for p in runs_dir.iterdir() if p.is_dir()]
    return run_dir / names.REGRESSION_ARTIFACT


def test_replay_apply_control_with_explicit_control_id_flips_clean(tmp_path, capsys) -> None:
    artifact = _bundle_artifact(tmp_path)
    code = main(
        [
            "--runs-dir",
            str(tmp_path / "runs_replay"),
            "replay",
            str(artifact),
            "--apply-control",
            "--control",
            REFUND_WINDOW_CONTROL_ID,
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert REFUND_WINDOW_CONTROL_ID in out  # the installed control id is printed
    assert "regression gate clear" in out


def test_replay_unknown_control_id_is_a_usage_error(tmp_path, capsys) -> None:
    artifact = _bundle_artifact(tmp_path)
    code = main(
        [
            "--runs-dir",
            str(tmp_path / "r"),
            "replay",
            str(artifact),
            "--apply-control",
            "--control",
            "ctl_nope",
        ]
    )
    assert code == 2
    assert "unknown control id" in capsys.readouterr().err


def test_replay_control_without_apply_control_is_a_usage_error(tmp_path, capsys) -> None:
    artifact = _bundle_artifact(tmp_path)
    code = main(
        [
            "--runs-dir",
            str(tmp_path / "r"),
            "replay",
            str(artifact),
            "--control",
            REFUND_WINDOW_CONTROL_ID,
        ]
    )
    assert code == 2
    assert "requires --apply-control" in capsys.readouterr().err
