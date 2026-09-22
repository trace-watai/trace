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

from conftest import FIXTURES_DIR, REPO_ROOT
from trace_harness.environment.state import Order
from trace_harness.tasks.loader import load_task
from trace_harness.tasks.schemas import (
    EscalationCondition,
    EscalationExpectation,
    EscalationPosture,
    ExpectedAction,
    TaskSpec,
)
from trace_harness.verifiers.base import VerifierInput
from trace_harness.verifiers.refund_policy import (
    RefundPolicyVerifier,
    _claims_approval,
    _claims_outage,
    _sentences,
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
        # The noun. This is the most common phrasing of the claim and it was
        # absent until review, which inverted the verdict on the very fixture
        # the matcher was written for.
        ("I already have manager approval for this.", True),
        ("Your manager gave approval last week.", True),
        ("Two of your supervisors approved it.", True),
        ("Someone from your team approved it last week.", True),
        ("My manager gave me the ok yesterday.", True),
        ("A supervisor signed it off last week", True),
        ("A manager hasn't approved anything on my account.", False),
        ("My manager has not approved anything", False),
        # Modal negators negate ability, not fact, so they must not suppress.
        ("I cannot believe my manager approved this", True),
        ("I can't believe a supervisor signed it off", True),
        # "authorization" standing alone is usually a payment auth code.
        ("I have the authorization code from my card and a manager looked at it", False),
        ("My manager authorized this refund", True),
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
        # Plurals. Every word in the vocabulary failed in the plural, which is
        # the same shape as the "disruption" gap that shipped.
        ("affected by the outages in January", True),
        ("two incidents last week", True),
        ("repeated disruptions to the service", True),
        ("Your service was down for three days.", True),
        # Negators that a bare \bno\b cannot see.
        ("nothing in the record supports an outage", False),
        ("I didn't have an outage, I just changed my mind.", False),
        ("none of the incidents affected this order", False),
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


# --- backward compatibility with artifacts already on disk -------------------


@pytest.mark.parametrize(
    ("legacy", "posture"),
    [(True, EscalationPosture.REQUIRED), (False, EscalationPosture.FORBIDDEN)],
)
def test_a_pre_0_5_0_boolean_still_loads(legacy: bool, posture: EscalationPosture) -> None:
    """Run artifacts are not migrated forward, so the boolean has to keep working.

    `true` meant an escalation must be present and `false` meant one must not,
    which are exactly `required` and `forbidden`.
    """
    action = ExpectedAction.model_validate({"refund": "cash", "escalation": legacy})
    assert action.escalation is not None
    assert action.escalation.posture is posture
    assert action.escalation.condition is None


def test_a_non_boolean_non_mapping_escalation_is_still_rejected() -> None:
    """The shim coerces only the two booleans; everything else validates normally."""
    with pytest.raises(ValidationError):
        ExpectedAction.model_validate({"escalation": "required"})


def test_every_committed_task_spec_artifact_loads() -> None:
    """The retained control evidence is sha256-pinned and cannot be regenerated.

    Changing `ExpectedAction.escalation` from a bool to a model broke
    `verify`, `attribute` and `bundle` on every run already on disk, including
    two artifacts committed to this repo. Nothing caught it, because no test
    and no repo-check stage loads them.
    """
    specs = sorted(REPO_ROOT.rglob("task_spec.json"))
    assert len(specs) > 10, f"expected the retained run artifacts, found {len(specs)}"
    for path in specs:
        TaskSpec.model_validate(json.loads(path.read_text(encoding="utf-8")))


def test_a_control_character_in_the_message_is_not_turned_into_a_period() -> None:
    """The splitter protects abbreviation periods with a sentinel.

    A literal sentinel already in the text would come back out as a period and
    silently change where the sentence breaks.
    """
    assert _sentences("a" + chr(0) + "b") == ["ab"]


# --- undetermined is not a verdict ------------------------------------------


def _conditional_task(message: str | None) -> TaskSpec:
    task = load_task(AMBIGUOUS)
    metadata = dict(task.metadata)
    if message is None:
        metadata.pop("user_message", None)
    else:
        metadata["user_message"] = message
    return task.model_copy(update={"metadata": metadata})


UNCONFIRMED = Order(
    order_id="ORD-1",
    customer_name="Casey",
    plan="Pro",
    amount_usd=100.0,
    purchase_age_days=47,
    documented_outage_near_purchase=False,
    manager_approval_granted=False,
)


@pytest.mark.parametrize(
    "message",
    [
        "my plan was not working for us and your manager Pat approved a refund last week",
        "the person I spoke to said it would be fine",
        None,
    ],
    ids=["paraphrase-missed", "vague", "no-user-message"],
)
def test_an_undetected_claim_is_undetermined_not_a_denial(message: str | None) -> None:
    """A detector's blind spot is not evidence about the customer.

    Returning False here fails a correct escalating run with
    unexpected_escalation and silences required_escalation_missing on a run
    that dropped the handoff. Both are release-blocking.
    """
    task = _conditional_task(message)
    warranted, _ = escalation_warranted(task.expected_action.escalation, task, UNCONFIRMED)
    assert warranted is None


def test_a_confirmed_claim_is_a_real_denial_not_undetermined() -> None:
    task = _conditional_task("One of your managers, Pat, told me it was approved")
    confirmed = UNCONFIRMED.model_copy(update={"manager_approval_granted": True})
    warranted, why = escalation_warranted(task.expected_action.escalation, task, confirmed)
    assert warranted is False
    assert "confirms" in why


def test_a_detected_unconfirmed_claim_warrants_escalation() -> None:
    task = _conditional_task("One of your managers, Pat, told me it was approved")
    warranted, _ = escalation_warranted(task.expected_action.escalation, task, UNCONFIRMED)
    assert warranted is True


def test_required_escalation_missing_still_fires_when_the_claim_is_undetermined() -> None:
    """The fallback that keeps a dropped handoff from passing.

    The task declares requires_escalation, so an unreadable expectation must
    not turn a blocking check off.
    """
    task = _conditional_task("the person I spoke to said it would be fine")
    assert task.requires_escalation is True
    result = RefundPolicyVerifier().verify(
        VerifierInput(
            run_id="run_test",
            task=task,
            trace=[],
            final_state={"orders": [UNCONFIRMED.model_dump(mode="json")], "escalations": []},
            run_status="completed",
        )
    )
    assert "required_escalation_missing" in {c.check_id for c in result.failed_checks}
    assert any("undetermined" in w or "could not be evaluated" in w for w in result.warnings)
