"""The controls #194 adds, one guardrail per prescribed repair control.

Each guardrail reads the same rule the verifier check it stands in for reads,
so these tests check the guardrail against that rule rather than restating it.
"""

from __future__ import annotations

import json

import pytest

from conftest import FIXTURES_DIR
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
)
from trace_harness.environment.support_env import SupportEnvironment
from trace_harness.models.base import ToolCall
from trace_harness.tasks.loader import load_task
from trace_harness.tasks.schemas import EscalationExpectation
from trace_harness.verifiers.refund_policy import claims_outage

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
    # A refund claim with no refund in state is blocked at the answer, not at a tool.
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


# --- ticket grounding and the shared labeled set -----------------------------


@pytest.mark.parametrize("case", LABELED, ids=[c["text"][:40] for c in LABELED])
def test_the_matcher_returns_what_the_labeled_set_records(case) -> None:
    """Where the matcher is known wrong, the case says so and pins today's answer."""
    assert claims_outage(case["text"]) is case["matcher"]


@pytest.mark.parametrize("case", LABELED, ids=[c["text"][:40] for c in LABELED])
def test_verifier_and_guardrail_agree_on_every_labeled_text(case) -> None:
    call = ToolCall(
        tool_name="create_ticket",
        arguments={"customer_name": "Casey", "title": "Refund request", "notes": case["text"]},
    )
    blocked = ticket_outage_claim_guardrail(call, _state(_order(45, outage=False)))
    assert (blocked is not None) is claims_outage(f"Refund request\n{case['text']}")


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


def test_citing_a_deprecated_doc_while_a_current_one_exists_is_blocked() -> None:
    call = ToolCall(
        tool_name="issue_refund",
        arguments={
            "customer_name": "Casey",
            "refund_type": "cash",
            "reason": "per refund_policy_v2",
        },
    )
    assert deprecated_policy_citation_guardrail(call, _state(_order(10), docs=_docs())) is not None


def test_citing_the_current_doc_is_allowed() -> None:
    call = ToolCall(
        tool_name="issue_refund",
        arguments={
            "customer_name": "Casey",
            "refund_type": "cash",
            "reason": "per refund_policy_v4",
        },
    )
    assert deprecated_policy_citation_guardrail(call, _state(_order(10), docs=_docs())) is None


def test_a_deprecated_doc_is_not_blocked_when_it_is_the_only_guidance() -> None:
    only_old = [_docs()[0]]
    call = ToolCall(
        tool_name="issue_refund",
        arguments={
            "customer_name": "Casey",
            "refund_type": "cash",
            "reason": "per refund_policy_v2",
        },
    )
    assert deprecated_policy_citation_guardrail(call, _state(_order(10), docs=only_old)) is None


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


def test_no_task_means_nothing_to_enforce() -> None:
    assert required_escalation_guardrail("Declined.", _state(_order(47)), None) is None
