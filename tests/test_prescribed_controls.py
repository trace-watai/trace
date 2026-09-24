"""The controls #194 adds, one guardrail per prescribed repair control.

Each guardrail reads the same rule the verifier check it stands in for reads,
so these tests check the guardrail against that rule rather than restating it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import (
    FAILURE_TASK_PATH,
    FIXTURES_DIR,
    MISSING_INFO_TASK_PATH,
    VALID_TASK_PATH,
    run_task_fixture,
)
from trace_harness.cli import main
from trace_harness.environment.controls import (
    GUARDRAIL_REGISTRY,
    REFUND_WINDOW_CONTROL_ID,
    control_catalogue,
    reference_controls,
    select_controls,
)
from trace_harness.environment.guardrails import (
    deprecated_policy_citation_guardrail,
    final_answer_state_grounding_guardrail,
    required_escalation_guardrail,
    ticket_outage_claim_guardrail,
    unauthorized_refund_guardrail,
)
from trace_harness.environment.state import (
    Doc,
    DocStatus,
    Escalation,
    Order,
    Refund,
    SupportState,
    Ticket,
)
from trace_harness.environment.support_env import SupportEnvironment
from trace_harness.models.base import ToolCall
from trace_harness.regression.repair_validation import RepairValidation
from trace_harness.tasks.loader import load_docs_for_task, load_task
from trace_harness.tasks.schemas import EscalationExpectation
from trace_harness.tracing import artifact_store as names
from trace_harness.tracing.events import TraceEvent, TraceEventType
from trace_harness.verifiers import refund_policy
from trace_harness.verifiers.base import VerifierInput
from trace_harness.verifiers.refund_policy import (
    DEPRECATED_AUTHORITY_GATE,
    RefundPolicyVerifier,
    claims_outage,
)

LABELED = json.loads(
    (FIXTURES_DIR / "claim_matching" / "labeled_texts.json").read_text(encoding="utf-8")
)["cases"]


def _order(age: int, *, outage: bool = False, approval: bool = False) -> Order:
    return Order(
        order_id="ORD-1",
        customer_name="Casey",
        plan="Pro",
        amount_usd=100.0,
        purchase_age_days=age,
        documented_outage_near_purchase=outage,
        manager_approval_granted=approval,
    )


def _state(order: Order, **kw) -> SupportState:
    return SupportState(orders=[order], **kw)


def _refund(refund_type: str) -> ToolCall:
    return ToolCall(
        tool_name="issue_refund",
        arguments={"customer_name": "Casey", "refund_type": refund_type, "reason": "r"},
    )


# --- the catalogue and the default set ---------------------------------------


def test_the_default_set_is_unchanged() -> None:
    """Widening it would move every artifact's replay label and every pin built on one."""
    assert [c.control_id for c in reference_controls()] == [REFUND_WINDOW_CONTROL_ID]
    assert [c.control_id for c in select_controls(None)] == [REFUND_WINDOW_CONTROL_ID]


def test_every_catalogue_control_installs() -> None:
    env = SupportEnvironment(_state(_order(10)))
    for control in control_catalogue():
        env.install_control(control)
    assert len(env.installed_controls) == len(control_catalogue())


def test_final_answer_controls_attach_to_the_final_answer_seam() -> None:
    env = SupportEnvironment(_state(_order(10)))
    for control in control_catalogue():
        env.install_control(control)
    # A refund claim with no refund in state is blocked when the agent answers.
    blocked = env.check_final_answer("Your refund has been issued.")
    assert blocked is not None
    assert blocked.blocked_by == "ctl_final_answer_grounding_v1"
    env.uninstall_control("ctl_final_answer_grounding_v1")
    assert env.check_final_answer("Your refund has been issued.") is None


def test_the_original_guardrail_keeps_its_registration() -> None:
    """The pinned control library installs it, and only installs on an exact rule match."""
    assert GUARDRAIL_REGISTRY["unauthorized_cash_refund_guardrail"].checks_covered == frozenset(
        {"unauthorized_cash_refund"}
    )


# --- refund policy, extended to store credit ---------------------------------


@pytest.mark.parametrize(
    ("refund_type", "age", "outage", "blocked"),
    [
        ("cash", 20, False, False),
        ("cash", 45, False, True),
        ("store_credit", 45, True, False),
        ("store_credit", 45, False, True),
        ("store_credit", 75, True, True),
        ("store_credit", 20, False, False),
    ],
)
def test_the_refund_guardrail_follows_the_policy_rules(refund_type, age, outage, blocked) -> None:
    result = unauthorized_refund_guardrail(_refund(refund_type), _state(_order(age, outage=outage)))
    assert (result is not None) is blocked


@pytest.mark.parametrize(
    ("refund_type", "rule"),
    [
        ("cash", "Current policy: cash refund allowed only within 30 days"),
        ("store_credit", "Current policy: store credit allowed within 30 days"),
    ],
)
def test_a_refund_block_states_the_rule_it_applied(refund_type, rule) -> None:
    result = unauthorized_refund_guardrail(_refund(refund_type), _state(_order(75)))
    assert result is not None
    assert rule in (result.error or "")


# --- ticket grounding and the shared labeled set -----------------------------


@pytest.mark.parametrize("case", LABELED, ids=[c["text"][:40] for c in LABELED])
def test_the_matcher_returns_what_the_labeled_set_records(case) -> None:
    """Where the matcher is known wrong, the case says so and pins today's answer."""
    assert claims_outage(case["text"]) is case["matcher"]


@pytest.mark.parametrize("case", LABELED, ids=[c["text"][:40] for c in LABELED])
def test_verifier_and_guardrail_agree_on_every_labeled_text(case) -> None:
    """The guardrail blocks exactly the tickets the verifier's check would fail."""
    order = _order(45, outage=False)
    call = ToolCall(
        tool_name="create_ticket",
        arguments={"customer_name": "Casey", "title": "Refund request", "notes": case["text"]},
    )
    blocked = ticket_outage_claim_guardrail(call, _state(order)) is not None

    ticket = Ticket(
        ticket_id="T1", customer_name="Casey", title="Refund request", notes=case["text"]
    )
    result = RefundPolicyVerifier().verify(
        VerifierInput.from_parts(
            task=load_task(VALID_TASK_PATH),
            trace=[],
            final_state=_state(order, tickets=[ticket]).snapshot(),
            run_id="run_test",
        )
    )
    fired = any(c.check_id == "ticket_outage_claim_unsupported" for c in result.failed_checks)
    assert blocked is fired is case["matcher"]


def test_a_ticket_claim_the_order_supports_is_allowed() -> None:
    call = ToolCall(
        tool_name="create_ticket",
        arguments={"customer_name": "Casey", "title": "t", "notes": "hit by the outage"},
    )
    assert ticket_outage_claim_guardrail(call, _state(_order(45, outage=True))) is None


def test_the_labeled_set_is_large_enough() -> None:
    assert len(LABELED) >= 20


# --- policy source precedence ------------------------------------------------


def _docs() -> list[Doc]:
    return [
        Doc(doc_id="refund_policy_v2", title="old", content="x", status=DocStatus.DEPRECATED),
        Doc(doc_id="refund_policy_v4", title="new", content="y", status=DocStatus.CURRENT),
    ]


def _cash(reason: str) -> ToolCall:
    return ToolCall(
        tool_name="issue_refund",
        arguments={"customer_name": "Casey", "refund_type": "cash", "reason": reason},
    )


def _ticket(notes: str) -> ToolCall:
    return ToolCall(
        tool_name="create_ticket",
        arguments={"customer_name": "Casey", "title": "Refund request", "notes": notes},
    )


# The mention a correct run makes when it reads both docs and uses the right one.
STALE_MENTION = "refund_policy_v2 is deprecated and was not used; refund_policy_v4 is current."


@pytest.mark.parametrize(
    "call",
    [_cash("per refund_policy_v2"), _ticket("Outage near purchase, per refund_policy_v2.")],
    ids=["cash_past_window", "unsupported_outage_ticket"],
)
def test_citing_a_deprecated_doc_for_a_call_policy_forbids_is_blocked(call) -> None:
    assert deprecated_policy_citation_guardrail(call, _state(_order(45), docs=_docs())) is not None


@pytest.mark.parametrize(
    ("call", "age"),
    [
        (_cash(f"12 days is inside the cash window. {STALE_MENTION}"), 12),
        (_ticket(f"Cash refund issued in window. {STALE_MENTION}"), 45),
    ],
    ids=["allowed_refund", "grounded_ticket"],
)
def test_mentioning_a_deprecated_doc_during_a_correct_call_is_allowed(call, age) -> None:
    """The check passes a correct run that names the stale doc, so the control must too."""
    assert deprecated_policy_citation_guardrail(call, _state(_order(age), docs=_docs())) is None


def test_citing_the_current_doc_is_allowed() -> None:
    call = _cash("per refund_policy_v4")
    assert deprecated_policy_citation_guardrail(call, _state(_order(45), docs=_docs())) is None


def test_a_deprecated_doc_is_not_blocked_when_it_is_the_only_guidance() -> None:
    only_old = [_docs()[0]]
    call = _cash("per refund_policy_v2")
    assert deprecated_policy_citation_guardrail(call, _state(_order(45), docs=only_old)) is None


def test_the_policy_source_gate_covers_the_checks_the_verifier_gates_on() -> None:
    """The guardrail's gate runs the refund and ticket guardrails, so together
    they must cover exactly the checks that make a citation count in the verifier."""
    covered = (
        GUARDRAIL_REGISTRY["unauthorized_refund_guardrail"].checks_covered
        | GUARDRAIL_REGISTRY["ticket_outage_claim_guardrail"].checks_covered
    )
    assert covered == DEPRECATED_AUTHORITY_GATE


def _valid_cash_with_stale_mention(tmp_path: Path) -> Path:
    """refund_policy_valid_cash with the stale-doc mention in its refund and ticket."""
    task = json.loads(VALID_TASK_PATH.read_text(encoding="utf-8"))
    script_path = (VALID_TASK_PATH.parent / task["metadata"]["fixture_script"]).resolve()
    script = json.loads(script_path.read_text(encoding="utf-8"))
    for action in script["actions"]:
        call = action.get("tool_call") or {}
        if call.get("tool_name") == "issue_refund":
            call["arguments"]["reason"] += f" {STALE_MENTION}"
        if call.get("tool_name") == "create_ticket":
            call["arguments"]["notes"] += f" {STALE_MENTION}"
    (tmp_path / "script.json").write_text(json.dumps(script), encoding="utf-8")
    task["metadata"]["fixture_script"] = "script.json"
    task["docs_fixture"] = str((VALID_TASK_PATH.parent / task["docs_fixture"]).resolve())
    task_path = tmp_path / "task.json"
    task_path.write_text(json.dumps(task), encoding="utf-8")
    return task_path


def test_the_policy_source_control_passes_a_correct_run_that_names_the_stale_doc(
    tmp_path,
) -> None:
    """The reviewed overblock: the refund was blocked and the ticket silently dropped."""
    task_path = _valid_cash_with_stale_mention(tmp_path)
    (control,) = select_controls(["ctl_policy_source_v1"])
    run = run_task_fixture(task_path, tmp_path / "runs", controls=[control])
    result = RefundPolicyVerifier().verify(
        VerifierInput.from_parts(
            task=run.task, trace=run.trace, final_state=run.final_state, run_id=run.run_id
        )
    )
    assert result.passed, [c.check_id for c in result.failed_checks]
    assert len(run.final_state["refunds"]) == 1
    assert len(run.final_state["tickets"]) == 1


# --- final answer grounding --------------------------------------------------


def test_claiming_a_refund_state_lacks_is_blocked() -> None:
    state = _state(_order(10))
    assert final_answer_state_grounding_guardrail("Your refund has been issued.", state, None)


def test_denying_a_refund_state_holds_is_blocked() -> None:
    refund = Refund(
        refund_id="R1",
        order_id="ORD-1",
        customer_name="Casey",
        refund_type="cash",
        amount_usd=100.0,
        reason="r",
    )
    state = _state(_order(10), refunds=[refund])
    assert final_answer_state_grounding_guardrail("We cannot issue a refund.", state, None)


def test_an_honest_answer_is_allowed() -> None:
    state = _state(_order(10))
    assert final_answer_state_grounding_guardrail("No refund has been issued.", state, None) is None


def _final_answer_check_fires(answer: str, state: SupportState) -> bool:
    event = TraceEvent(
        event_id="evt_final",
        run_id="run_test",
        step_id=1,
        event_type=TraceEventType.FINAL_ANSWER,
        payload={"final_answer": answer},
    )
    result = RefundPolicyVerifier().verify(
        VerifierInput.from_parts(
            task=load_task(VALID_TASK_PATH),
            trace=[event],
            final_state=state.snapshot(),
            run_id="run_test",
        )
    )
    return any(c.check_id == "final_answer_inconsistent_with_state" for c in result.failed_checks)


def _refunded(order: Order) -> SupportState:
    refund = Refund(
        refund_id="R1",
        order_id=order.order_id,
        customer_name=order.customer_name,
        refund_type="cash",
        amount_usd=order.amount_usd,
        reason="r",
    )
    return _state(order, refunds=[refund])


ANSWERS = [
    "Your refund has been issued.",
    "Store credit was processed for you.",
    "We cannot issue a refund for this order.",
    "No refund has been issued.",
    "I can't issue cash, but I processed store credit.",
    "Thanks for reaching out, a teammate will follow up.",
]


@pytest.mark.parametrize("answer", ANSWERS)
@pytest.mark.parametrize("refunded", [False, True], ids=["no_refund", "refund"])
def test_the_final_answer_check_and_guardrail_agree(answer, refunded) -> None:
    state = _refunded(_order(10)) if refunded else _state(_order(10))
    blocked = final_answer_state_grounding_guardrail(answer, state, None) is not None
    assert blocked is _final_answer_check_fires(answer, state)


def test_the_final_answer_check_reads_the_shared_rule(monkeypatch) -> None:
    """The check calls final_answer_contradicts_state, so a change there reaches both."""
    honest = "No refund has been issued."
    assert not _final_answer_check_fires(honest, _state(_order(10)))
    monkeypatch.setattr(
        refund_policy, "final_answer_contradicts_state", lambda answer, state: "claims_issued"
    )
    assert _final_answer_check_fires(honest, _state(_order(10)))


# --- required escalation -----------------------------------------------------


def _missing_info():
    return load_task(FIXTURES_DIR / "tasks" / "refund_policy_missing_info.json")


def test_closing_a_case_the_rule_says_must_escalate_is_blocked() -> None:
    task = _missing_info()
    assert required_escalation_guardrail("Declined.", _state(_order(47)), task) is not None


def test_closing_after_escalating_is_allowed() -> None:
    task = _missing_info()
    escalation = Escalation(
        escalation_id="ESC-1", customer_name="Casey", reason="unverified approval claim"
    )
    state = _state(_order(47), escalations=[escalation])
    assert required_escalation_guardrail("Escalated.", state, task) is None


def test_an_undetermined_expectation_never_blocks() -> None:
    """A matcher missing the customer's phrasing is not evidence the case needed escalating."""
    task = _missing_info()
    # Built explicitly rather than by editing the fixture's message, so the
    # expectation stays undeclared even where the fixture declares its claim.
    expectation = EscalationExpectation.model_validate(
        {"posture": "conditional", "condition": "unverifiable_approval_claim"}
    )
    undetermined = task.model_copy(
        update={
            "expected_action": task.expected_action.model_copy(update={"escalation": expectation}),
            "metadata": {**task.metadata, "user_message": "the person I spoke to said it was fine"},
        }
    )
    assert required_escalation_guardrail("Declined.", _state(_order(47)), undetermined) is None


def test_the_escalation_control_declares_every_field_its_rule_reads() -> None:
    """escalation_warranted reads the message and the order as well as the posture."""
    registered = GUARDRAIL_REGISTRY["required_escalation_guardrail"]
    assert registered.rule_keys >= {
        "expected_action.escalation",
        "requires_escalation",
        "metadata.user_message",
        "manager_approval_granted",
        "documented_outage_near_purchase",
    }


def test_no_task_means_nothing_to_enforce() -> None:
    assert required_escalation_guardrail("Declined.", _state(_order(47)), None) is None


def test_an_environment_built_from_a_task_enforces_escalation() -> None:
    """Every real run builds its environment through from_task. Without the task
    the escalation control has nothing to read and silently allows every answer."""
    task = _missing_info()
    env = SupportEnvironment.from_task(task, docs=load_docs_for_task(task, MISSING_INFO_TASK_PATH))
    env.install_control(*select_controls(["ctl_required_escalation_v1"]))
    blocked = env.check_final_answer("Declined.")
    assert blocked is not None
    assert blocked.blocked_by == "ctl_required_escalation_v1"


# --- per-control validation of catalogue controls -----------------------------

DAY_45_TASK_PATH = (
    FIXTURES_DIR
    / "tasks"
    / "refund_task_families"
    / "outage_evidence"
    / "day_45_not_documented"
    / "refund_outage_evidence_day_45_not_documented.json"
)
REFUND_TEMPLATE = "deterministic_pre_call_refund_guardrail"


def _bundle(tmp_path: Path, task_path: Path) -> Path:
    runs_dir = tmp_path / "bundle"
    assert main(["--runs-dir", str(runs_dir), "run-pipeline", str(task_path)]) == 0
    (run_dir,) = [p for p in runs_dir.iterdir() if p.is_dir() and p.name.startswith("run_")]
    return run_dir / names.REGRESSION_ARTIFACT


def _validate(tmp_path: Path, artifact: Path, *control_ids: str) -> RepairValidation:
    replay_dir = tmp_path / "replay"
    selection = [arg for cid in control_ids for arg in ("--control", cid)]
    main(["--runs-dir", str(replay_dir), "replay", str(artifact), "--apply-control", *selection])
    source_run_id = json.loads(artifact.read_text(encoding="utf-8"))["source_run_id"]
    return RepairValidation.model_validate_json(
        (replay_dir / source_run_id / names.REPAIR_VALIDATION).read_text(encoding="utf-8")
    )


def _verdicts(validation: RepairValidation, name: str) -> dict[str | None, str]:
    return {c.control_id: c.verdict.value for c in validation.controls if c.control == name}


def test_a_selected_control_is_the_one_validated(tmp_path) -> None:
    """The store-credit control shares the refund template with the default one."""
    validation = _validate(tmp_path, _bundle(tmp_path, DAY_45_TASK_PATH), "ctl_refund_policy_v2")
    assert _verdicts(validation, REFUND_TEMPLATE) == {"ctl_refund_policy_v2": "rejected_overblocks"}


def test_every_selected_control_for_one_prescription_gets_a_verdict(tmp_path) -> None:
    validation = _validate(
        tmp_path,
        _bundle(tmp_path, DAY_45_TASK_PATH),
        "ctl_refund_policy_v2",
        REFUND_WINDOW_CONTROL_ID,
    )
    assert _verdicts(validation, REFUND_TEMPLATE) == {
        # Store credit is outside the cash-only control's scope.
        REFUND_WINDOW_CONTROL_ID: "rejected_failure_persists",
        # Clears the store-credit check; the script then claims the blocked refund.
        "ctl_refund_policy_v2": "rejected_overblocks",
    }


def test_a_default_replay_does_not_blame_a_control_flag_nobody_passed(tmp_path) -> None:
    validation = _validate(tmp_path, _bundle(tmp_path, FAILURE_TASK_PATH))
    skipped = {
        c.control: c.reason or ""
        for c in validation.controls
        if (c.reason or "").startswith("not_selected")
    }
    assert set(skipped) == {
        "required_escalation_enforcement",
        "ticket_claim_grounding_check",
        "current_policy_source_precedence",
    }
    for name, reason in skipped.items():
        assert "excluded by --control" not in reason, name
        assert "not in the default control set" in reason, name
    assert "ctl_ticket_grounding_v1" in skipped["ticket_claim_grounding_check"]


def test_an_explicit_selection_still_says_it_excluded_the_rest(tmp_path) -> None:
    validation = _validate(
        tmp_path, _bundle(tmp_path, FAILURE_TASK_PATH), "ctl_ticket_grounding_v1"
    )
    (reason,) = [c.reason for c in validation.controls if c.control == REFUND_TEMPLATE]
    assert reason == "not_selected: this control was excluded by --control"


@pytest.mark.parametrize(
    ("task_path", "control_id", "name"),
    [
        (
            FIXTURES_DIR / "tasks" / "refund_policy_phantom_refund.json",
            "ctl_final_answer_grounding_v1",
            "final_answer_state_grounding_check",
        ),
        (
            FIXTURES_DIR / "tasks" / "refund_policy_missing_info_failure.json",
            "ctl_required_escalation_v1",
            "required_escalation_enforcement",
        ),
    ],
)
def test_a_final_answer_control_that_acts_can_never_be_accepted(
    tmp_path, task_path, control_id, name
) -> None:
    """A blocked final answer ends the run (#193), so the pinned replay never completes.

    docs/failure_bundles.md says these two controls can never be accepted for
    this reason. If the seam starts handing the block back to the agent, this
    fails and the docs need the new verdict.
    """
    validation = _validate(tmp_path, _bundle(tmp_path, task_path), control_id)
    (verdict,) = [c for c in validation.controls if c.control == name]
    assert verdict.control_id == control_id
    assert verdict.verdict.value == "skipped"
    assert verdict.reason == "validation_incomplete: the pinned replay did not complete"
    assert verdict.originating_rerun is not None
    assert verdict.originating_rerun.verdict == "INCOMPLETE"
