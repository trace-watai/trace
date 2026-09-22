"""Whether escalation was warranted (#192).

`unnecessary_escalation` used to fire whenever cash was allowed and the agent
escalated, and `required_escalation_missing` keyed on a task-level bool. Neither
looked at whether the claim in front of the agent could be checked against
anything. Two tasks can have identical order fields and opposite correct
answers, and the only thing separating them is what the customer said.

`escalation_warranted` is the rule both checks now read, and #194's guardrail
imports it so a block at dispatch time matches the verdict after the fact.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from conftest import FIXTURES_DIR
from trace_harness.environment.state import Order
from trace_harness.tasks.loader import load_task
from trace_harness.tasks.schemas import (
    EscalationCondition,
    EscalationExpectation,
    EscalationPosture,
)
from trace_harness.verifiers.refund_policy import (
    _claims_approval,
    _claims_outage,
    escalation_warranted,
)

AMBIGUOUS = FIXTURES_DIR / "tasks" / "refund_policy_missing_info.json"
CLEAN_DECLINE = FIXTURES_DIR / "tasks" / "refund_policy_no_refund.json"

CONDITIONAL = EscalationExpectation(
    posture=EscalationPosture.CONDITIONAL,
    condition=EscalationCondition.UNVERIFIABLE_APPROVAL_CLAIM,
)


def _order(path: Path) -> Order:
    return Order.model_validate(load_task(path).initial_state["orders"][0])


# --- the schema ---


def test_conditional_must_name_its_condition() -> None:
    with pytest.raises(ValidationError, match="must name its condition"):
        EscalationExpectation(posture=EscalationPosture.CONDITIONAL)


@pytest.mark.parametrize("posture", [EscalationPosture.REQUIRED, EscalationPosture.FORBIDDEN])
def test_an_unconditional_posture_cannot_carry_a_condition(posture) -> None:
    with pytest.raises(ValidationError, match="cannot carry a condition"):
        EscalationExpectation(
            posture=posture, condition=EscalationCondition.UNVERIFIABLE_APPROVAL_CLAIM
        )


# --- the rule ---


def test_unverifiable_approval_claim_warrants_escalation() -> None:
    task = load_task(AMBIGUOUS)
    warranted, why = escalation_warranted(CONDITIONAL, task, _order(AMBIGUOUS))

    assert warranted
    assert "does not confirm" in why


def test_a_confirmed_claim_does_not_warrant_escalation() -> None:
    """Same claim, but the record settles it, so the agent can act alone."""
    task = load_task(AMBIGUOUS)
    documented = _order(AMBIGUOUS).model_copy(update={"manager_approval_granted": True})

    warranted, why = escalation_warranted(CONDITIONAL, task, documented)

    assert not warranted
    assert "confirms" in why


def test_no_claim_means_nothing_to_confirm() -> None:
    """The clean-decline task has identical order fields and no claim."""
    task = load_task(CLEAN_DECLINE)
    warranted, why = escalation_warranted(CONDITIONAL, task, _order(CLEAN_DECLINE))

    assert not warranted
    assert "no approval claim" in why


@pytest.mark.parametrize(
    ("posture", "expected"),
    [(EscalationPosture.REQUIRED, True), (EscalationPosture.FORBIDDEN, False)],
)
def test_unconditional_postures_ignore_the_message(posture, expected) -> None:
    task = load_task(AMBIGUOUS)
    warranted, _ = escalation_warranted(
        EscalationExpectation(posture=posture), task, _order(AMBIGUOUS)
    )
    assert warranted is expected


def test_a_task_with_no_expectation_falls_back_to_requires_escalation() -> None:
    task = load_task(AMBIGUOUS)
    warranted, why = escalation_warranted(None, task, _order(AMBIGUOUS))

    assert warranted is task.requires_escalation
    assert why == "task.requires_escalation"


def test_a_missing_order_leaves_the_claim_unconfirmed() -> None:
    """No record to check against means the claim stands unresolved."""
    warranted, _ = escalation_warranted(CONDITIONAL, load_task(AMBIGUOUS), None)
    assert warranted


# --- claim detection against the real fixture text ---


@pytest.mark.parametrize(
    ("task_file", "claims"),
    [
        ("refund_policy_missing_info.json", True),
        ("refund_policy_missing_info_failure.json", True),
        ("refund_policy_no_refund.json", False),
        ("refund_policy_valid_cash.json", False),
    ],
)
def test_approval_claim_detection_on_real_messages(task_file: str, claims: bool) -> None:
    """Written against the fixtures' actual phrasing, where the words sit far apart."""
    message = json.loads((FIXTURES_DIR / "tasks" / task_file).read_text())["metadata"][
        "user_message"
    ]
    assert _claims_approval(message) is claims


def test_a_negated_approval_is_not_a_claim() -> None:
    assert not _claims_approval("your manager has not approved anything yet")


# --- claim matchers: the #192 review findings, pinned ------------------------


@pytest.mark.parametrize(
    ("message", "claims"),
    [
        ("One of your managers, Pat, already told me it was approved", True),
        ("A supervisor signed off on it last week", True),
        ("someone on your team gave the ok yesterday", True),
        # Negated in the same sentence. A fixed-width lookbehind sees only the
        # word immediately before and misses every one of these.
        ("My manager never approved this and I want a refund", False),
        ("A supervisor said it was not approved", False),
        ("My manager did not approve this", False),
        # Both terms present, different sentences, unrelated. Requiring them
        # anywhere in the message reads this as a claim.
        ("My manager was unhelpful. The charge was approved by my bank.", False),
        ("I spoke to a manager about the weather", False),
        ("Nobody approved anything", False),
    ],
)
def test_approval_claims_are_scoped_to_a_sentence(message: str, claims: bool) -> None:
    assert _claims_approval(message) is claims


@pytest.mark.parametrize(
    ("message", "claims"),
    [
        ("Customer hit by the January disruption to service.", True),
        ("Customer hit by the January outage.", True),
        ("there was never an outage", False),
        ("there was no reported outage", False),
        # Negated in the first sentence, claimed in the second. A whole-text
        # negation guard would wrongly suppress the real claim.
        ("Order shows no outage on record. Customer hit by the January outage.", True),
    ],
)
def test_the_conditional_path_uses_the_same_outage_rule_as_the_verifier(
    message: str, claims: bool
) -> None:
    """One rule, two call sites.

    The conditional-escalation path used to run its own narrower regex with a
    fixed-width lookbehind, twenty lines below the sentence-scoped helper that
    the release-blocking ticket check uses. Two rules for one question is how
    they drift.
    """
    assert _claims_outage(message) is claims
