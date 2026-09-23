"""Controls as data (TRA-87): registry, install/uninstall, selection, parity, CLI.

Unit tests build a bare SupportEnvironment the way test_guardrails.py builds a
bare SupportState; the CLI tests reuse the control-demo fixture the way
test_replay_control_flip.py does, so the registry path is proven equivalent to
the direct-import path it replaces.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from conftest import FIXTURES_DIR, VALID_TASK_PATH, run_task_fixture
from trace_harness.cli import main
from trace_harness.environment.controls import (
    GUARDRAIL_REGISTRY,
    MATERIALIZABLE_REPAIR_CONTROLS,
    REFUND_WINDOW_CONTROL_ID,
    ControlInstance,
    RegisteredGuardrail,
    RuleRef,
    RuleRefMismatchError,
    UnknownGuardrailError,
    guardrail_ref_for_repair_control,
    reference_controls,
    resolve_guardrail,
    select_controls,
)
from trace_harness.environment.guardrails import unauthorized_cash_refund_guardrail
from trace_harness.environment.state import Order, SupportState
from trace_harness.environment.support_env import SupportEnvironment
from trace_harness.environment.tools import ToolResult
from trace_harness.failure_bundles import generator as bundle_generator
from trace_harness.models.base import ToolCall
from trace_harness.tracing import artifact_store as names
from trace_harness.tracing.artifact_store import ArtifactStore
from trace_harness.tracing.events import TraceEventType

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


_REFUND_RULES = ["cash_refund_window_days", "manager_approval_extends_cash_to_days"]


def _instance(
    control_id: str = "ctl_test", guardrail_ref: str = "unauthorized_cash_refund_guardrail"
):
    return ControlInstance(
        control_id=control_id,
        guardrail_ref=guardrail_ref,
        rule_ref=RuleRef(source="current_policy_doc", rules=_REFUND_RULES),
    )


# --- registry ---


def test_registry_resolves_shipped_guardrail() -> None:
    registered = resolve_guardrail("unauthorized_cash_refund_guardrail")
    assert registered.fn is unauthorized_cash_refund_guardrail
    assert registered.rule_source == "current_policy_doc"
    assert registered.rule_keys == set(_REFUND_RULES)
    assert "unauthorized_cash_refund_guardrail" in GUARDRAIL_REGISTRY


def test_unknown_guardrail_ref_is_a_model_value_but_fails_at_install() -> None:
    instance = _instance(guardrail_ref="does_not_exist")  # the model itself accepts it
    env = _env_with_late_order()
    with pytest.raises(UnknownGuardrailError, match="does_not_exist"):
        env.install_control(instance)
    assert env.installed_controls == []
    # and dispatch is untouched: no hook was half-registered
    assert env.execute(_cash_call()).status == "ok"


# --- rule_ref must match what the guardrail reads ---


@pytest.mark.parametrize(
    "rule_ref",
    [
        pytest.param(
            RuleRef(source="deprecated_policy_doc", rules=_REFUND_RULES), id="wrong_source"
        ),
        pytest.param(
            RuleRef(source="current_policy_doc", rules=["cash_refund_window_days"]),
            id="missing_key",
        ),
        pytest.param(
            RuleRef(source="current_policy_doc", rules=[*_REFUND_RULES, "not_read_by_guardrail"]),
            id="extra_key",
        ),
    ],
)
def test_rule_ref_mismatch_fails_at_install(rule_ref: RuleRef) -> None:
    env = _env_with_late_order()
    with pytest.raises(RuleRefMismatchError, match="does not match guardrail"):
        env.install_control(_instance().model_copy(update={"rule_ref": rule_ref}))
    assert env.installed_controls == []
    assert env.execute(_cash_call()).status == "ok"  # nothing half-registered


def test_rule_ref_key_order_does_not_matter() -> None:
    env = _env_with_late_order()
    reordered = RuleRef(source="current_policy_doc", rules=list(reversed(_REFUND_RULES)))
    env.install_control(_instance().model_copy(update={"rule_ref": reordered}))
    assert [c.control_id for c in env.installed_controls] == ["ctl_test"]


def test_every_reference_control_installs() -> None:
    # a shipped control whose rule_ref drifts from its guardrail fails here
    env = _env_with_late_order()
    for control in reference_controls():
        env.install_control(control)
    assert [c.control_id for c in env.installed_controls] == [
        c.control_id for c in reference_controls()
    ]


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
    # identical except that the control stamps itself on the block
    assert via_control.blocked_by == REFUND_WINDOW_CONTROL_ID
    assert via_control.model_dump(exclude={"blocked_by"}) == direct.model_dump(
        exclude={"blocked_by"}
    )


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
    assert guardrail_ref_for_repair_control("current_policy_source_precedence") == (
        "deprecated_policy_citation_guardrail"
    )
    # Still unbuilt after #194: detection controls and the CI gate.
    for name in (
        "regression_test_ci_gate",
        "expected_action_contract_check",
        "escalation_discipline_check",
        "retrieval_before_action_check",
    ):
        assert guardrail_ref_for_repair_control(name) is None, name
    assert guardrail_ref_for_repair_control("not_a_control") is None


def test_every_mapped_guardrail_is_registered_and_installable() -> None:
    """A map entry naming a guardrail nothing can install would count as coverage."""
    from trace_harness.environment.controls import GUARDRAIL_REGISTRY, control_catalogue

    catalogue = control_catalogue()
    for name, ref in MATERIALIZABLE_REPAIR_CONTROLS.items():
        if ref is None:
            continue
        assert ref in GUARDRAIL_REGISTRY, (name, ref)
        assert any(c.provenance.repair_control == name for c in catalogue), name


# --- positive sibling gate ---


def _block_every_refund(call: ToolCall, state: SupportState) -> ToolResult | None:
    if call.tool_name != "issue_refund":
        return None
    return ToolResult(tool_name="issue_refund", status="error", error="blocked: every refund")


def _sibling_failed_checks(tmp_path, controls: list[ControlInstance]) -> list[str]:
    runs_dir = tmp_path / "runs"
    run = run_task_fixture(VALID_TASK_PATH, runs_dir, controls=controls)
    main(["--runs-dir", str(runs_dir), "verify", str(run.store.run_dir(run.run_id))])
    verdict = run.store.read_json(run.run_id, names.VERIFIER_RESULT)
    return [check["check_id"] for check in verdict["failed_checks"]]


def test_reference_control_keeps_positive_sibling_passing(tmp_path) -> None:
    assert _sibling_failed_checks(tmp_path, reference_controls()) == []


def test_overblocking_control_fails_positive_sibling(tmp_path, monkeypatch) -> None:
    """Tripwire: the sibling gate must catch a control that blocks a legitimate refund.

    Both checks matter. ``final_answer_inconsistent_with_state`` catches this
    scripted sibling because it still claims the refund it never got, and
    ``expected_refund_missing`` (TRA-80) catches the block itself against the
    task's declared ``expected_action``. The second one is what would still
    fire for an agent that refused politely instead of claiming success, which
    is the case this tripwire could not cover before TRA-80.
    """
    monkeypatch.setitem(
        GUARDRAIL_REGISTRY,
        "block_every_refund",
        RegisteredGuardrail(fn=_block_every_refund, rule_source="none", rule_keys=frozenset()),
    )
    overblocking = ControlInstance(
        control_id="ctl_block_every_refund",
        guardrail_ref="block_every_refund",
        rule_ref=RuleRef(source="none"),
    )
    assert sorted(_sibling_failed_checks(tmp_path, [overblocking])) == [
        "expected_refund_missing",
        "final_answer_inconsistent_with_state",
    ]


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


def test_replay_with_control_writes_blocked_by_into_trace(tmp_path) -> None:
    """A control block is machine-identifiable in trace.jsonl and names its control."""
    artifact = _bundle_artifact(tmp_path)
    replay_dir = tmp_path / "runs_replay"
    assert main(["--runs-dir", str(replay_dir), "replay", str(artifact), "--apply-control"]) == 0

    store = ArtifactStore(replay_dir)
    # replay re-runs the scenario for the bundle gate and again per control
    # (#146), and run ids are not ordered by when they ran, so find the run
    # that recorded the block rather than assuming which one it is.
    tool_types = {TraceEventType.TOOL_CALL_EXECUTED, TraceEventType.TOOL_OBSERVATION}
    blocked_runs = {
        rid: [e for e in store.read_trace(rid) if e.event_type in tool_types]
        for rid in store.list_runs()
        if store.exists(rid, names.TRACE)
    }
    blocked_runs = {
        rid: events
        for rid, events in blocked_runs.items()
        if any(e.payload["blocked_by"] is not None for e in events)
    }
    assert blocked_runs, "no replayed run recorded a control block"
    run_id, tool_events = next(iter(blocked_runs.items()))
    blocked = [e for e in tool_events if e.payload["blocked_by"] is not None]

    # the refund step's executed + observation events, both naming the control
    assert {e.event_type for e in blocked} == tool_types
    assert len({e.step_id for e in blocked}) == 1
    for event in blocked:
        assert event.payload["tool_name"] == "issue_refund"
        assert event.payload["status"] == "error"
        assert event.typed_payload.blocked_by == REFUND_WINDOW_CONTROL_ID
    # every other tool event carries an explicit null, not a missing key
    assert all(e.payload["blocked_by"] is None for e in tool_events if e not in blocked)


def test_replay_unknown_control_id_is_a_usage_error(tmp_path, capsys) -> None:
    artifact = _bundle_artifact(tmp_path)
    capsys.readouterr()  # drop the run-pipeline output
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
    captured = capsys.readouterr()
    assert code == 2
    assert "unknown control id" in captured.err
    assert captured.out == ""  # fails before the replay header prints


def test_replay_control_without_apply_control_is_a_usage_error(tmp_path, capsys) -> None:
    artifact = _bundle_artifact(tmp_path)
    capsys.readouterr()  # drop the run-pipeline output
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
    captured = capsys.readouterr()
    assert code == 2
    assert "requires --apply-control" in captured.err
    assert captured.out == ""  # fails before the replay header prints


# --- unknown fields are rejected (environment/ convention: extra="forbid") ---


def test_unknown_field_in_control_record_is_rejected() -> None:
    with pytest.raises(ValidationError, match="guardrail_reg"):
        ControlInstance.model_validate(
            {
                "control_id": "ctl_typo",
                "guardrail_reg": "unauthorized_cash_refund_guardrail",
                "rule_ref": {"source": "current_policy_doc", "rules": []},
            }
        )
    with pytest.raises(ValidationError, match="sources"):
        RuleRef.model_validate({"sources": "current_policy_doc"})


# --- conflict detection at install (#193) ---


def _behavior(action: str):
    from trace_harness.environment.controls import BehaviorOnFailure

    return BehaviorOnFailure.model_construct(action=action)


def test_two_controls_reading_the_same_rules_with_different_behavior_conflict() -> None:
    """Ordering would decide the outcome, and nobody decided the ordering."""
    from trace_harness.environment.controls import ControlConflictError

    env = _env_with_late_order()
    first = _instance(control_id="ctl_block")
    env.install_control(first)

    second = _instance(control_id="ctl_warn").model_copy(
        update={"behavior_on_failure": _behavior("warn")}
    )
    with pytest.raises(ControlConflictError) as exc:
        env.install_control(second)

    message = str(exc.value)
    assert "ctl_block" in message and "ctl_warn" in message
    assert [c.control_id for c in env.installed_controls] == ["ctl_block"]


def test_same_guardrail_and_same_behavior_is_redundant_not_conflicting() -> None:
    """Two controls that agree are allowed; #147 owns ordering, this owns disagreement."""
    env = _env_with_late_order()
    env.install_control(_instance(control_id="ctl_a"))
    env.install_control(_instance(control_id="ctl_b"))

    assert [c.control_id for c in env.installed_controls] == ["ctl_a", "ctl_b"]


def test_different_rules_do_not_conflict() -> None:
    """Different rules mean different questions, so different answers are fine."""
    from trace_harness.environment.controls import RuleRef, find_conflict

    a = _instance(control_id="ctl_a")
    b = _instance(control_id="ctl_b").model_copy(
        update={
            "rule_ref": RuleRef(source="current_policy_doc", rules=["something_else"]),
            "behavior_on_failure": _behavior("warn"),
        }
    )
    assert find_conflict(b, [a]) is None
