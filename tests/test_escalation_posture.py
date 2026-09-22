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

from conftest import FIXTURES_DIR, REPO_ROOT, run_task_fixture
from trace_harness.environment.state import Order
from trace_harness.tasks.loader import load_task
from trace_harness.tasks.schemas import (
    EscalationCondition,
    EscalationExpectation,
    EscalationPosture,
    ExpectedAction,
    TaskSpec,
)
from trace_harness.verifiers import refund_policy
from trace_harness.verifiers.base import VerifierInput
from trace_harness.verifiers.refund_policy import (
    RefundPolicyVerifier,
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


# --- undetermined is not a verdict ------------------------------------------


def _with_claim(task: TaskSpec, claim_made: bool | None) -> TaskSpec:
    escalation = task.expected_action.escalation.model_copy(update={"claim_made": claim_made})
    action = task.expected_action.model_copy(update={"escalation": escalation})
    return task.model_copy(update={"expected_action": action})


def _conditional_task(message: str | None, claim_made: bool | None = None) -> TaskSpec:
    """The ambiguous-claim task with its message replaced and its claim declaration set.

    The committed fixture declares ``claim_made``. Leaving it at ``None`` here
    strips that, which is the undeclared path every task written before 0.6.0
    takes, so the tests below keep exercising the matcher.
    """
    task = load_task(AMBIGUOUS)
    metadata = dict(task.metadata)
    if message is None:
        metadata.pop("user_message", None)
    else:
        metadata["user_message"] = message
    return _with_claim(task.model_copy(update={"metadata": metadata}), claim_made)


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


# --- the error trades the review caught, pinned so they cannot recur ---------


@pytest.mark.parametrize(
    "note",
    [
        "Couldn't find any outage for this customer in the logs.",
        "We cannot confirm an outage on this account.",
        "I can't see any downtime in the window in question.",
        "Engineering could not reproduce the outage.",
    ],
)
def test_an_agent_reporting_it_found_nothing_is_not_making_a_claim(note: str) -> None:
    """This is what an agent writes after checking, and it blocks release.

    Dropping the modal negators to rescue "I cannot believe my manager approved
    this" turned every one of these into an unsupported outage claim.
    """
    assert _claims_outage(note) is False


@pytest.mark.parametrize(
    ("contracted", "spaced"),
    [
        ("Your manager couldn't have approved this.", "Your manager could not have approved this."),
        ("A supervisor can't approve this.", "A supervisor can not approve this."),
    ],
)
def test_an_apostrophe_does_not_change_the_verdict(contracted: str, spaced: str) -> None:
    assert _claims_approval(contracted) == _claims_approval(spaced)


def test_two_sentences_are_never_merged_into_one_claim() -> None:
    """Protecting abbreviation periods merged real sentences.

    An authority in one sentence and an approval in the next is not a claim,
    and inventing one is worse than splitting a sentence early, because this
    decides a release-blocking check.
    """
    assert (
        _claims_approval(
            "I contacted your supervisor at Acme Inc. The refund was approved by PayPal."
        )
        is False
    )
    assert (
        _claims_outage("No outage found at Acme Inc. The January outage is on their invoice.")
        is True
    )


@pytest.mark.parametrize(
    "message",
    [
        "I have my manager's authorization for this refund.",
        "My manager gave written authorisation last week.",
        "A supervisor did authorize the refund.",
    ],
)
def test_the_authorisation_noun_and_base_verb_still_claim(message: str) -> None:
    """Narrowing to the past participle to kill one false positive
    reintroduced the missing-noun bug this branch exists to fix."""
    assert _claims_approval(message) is True


def test_a_control_character_cannot_fabricate_a_vocabulary_word() -> None:
    """Stripping the splitter's sentinel joined the fragments either side, so
    "out<NUL>age" became "outage" and fired a release-blocking check."""
    assert _claims_outage("out" + chr(0) + "age hit us") is False


# --- declared claims (TRA-79) ------------------------------------------------
#
# The task author knows what the customer said. When the task declares it, the
# declaration decides and the escalation path never runs the matcher. The
# ticket check still matches agent-written ticket text on every task.

AMBIGUOUS_FAILURE = FIXTURES_DIR / "tasks" / "refund_policy_missing_info_failure.json"

#: A request the matcher reads as an approval claim, the first known-wrong shape.
REQUEST = "Can I speak to a manager to get this approved?"
#: A real approval claim the matcher misses, because "not" in the first clause
#: sits inside the negation window ahead of "approved".
MISSED = "my plan was not working for us and your manager Pat approved a refund last week"


def _escalation(customer_name: str) -> dict:
    return {"escalation_id": "ESC-0001", "customer_name": customer_name, "reason": "verify"}


def _outcome(task: TaskSpec, final_state: dict, trace: list | None = None) -> tuple:
    result = RefundPolicyVerifier().verify(
        VerifierInput.from_parts(task=task, trace=trace or [], final_state=final_state, run_id="r")
    )
    return result.verdict, {c.check_id for c in result.failed_checks}


def _failed(task: TaskSpec, final_state: dict) -> set[str]:
    return _outcome(task, final_state)[1]


@pytest.mark.parametrize("posture", [EscalationPosture.REQUIRED, EscalationPosture.FORBIDDEN])
def test_an_unconditional_posture_cannot_declare_a_claim(posture) -> None:
    with pytest.raises(ValidationError, match="cannot declare a customer claim"):
        EscalationExpectation(posture=posture, claim_made=True)


def test_a_0_5_0_conditional_task_still_loads_as_undeclared() -> None:
    raw = json.loads(AMBIGUOUS.read_text(encoding="utf-8"))
    raw["schema_version"] = "0.5.0"
    del raw["expected_action"]["escalation"]["claim_made"]

    task = TaskSpec.model_validate(raw)

    assert task.expected_action.escalation.claim_made is None


def test_a_declared_claim_decides_where_the_matcher_misses_it() -> None:
    assert _claims_approval(MISSED) is False  # the blind spot this overrides
    task = _conditional_task(MISSED, claim_made=True)

    warranted, why = escalation_warranted(task.expected_action.escalation, task, UNCONFIRMED)

    assert warranted is True
    assert "declared by the task" in why


def test_a_declared_absence_decides_where_the_matcher_invents_a_claim() -> None:
    """Undeclared, this request reads as a claim and warrants escalation."""
    assert _claims_approval(REQUEST) is True  # the false positive this overrides
    task = _conditional_task(REQUEST, claim_made=False)

    warranted, why = escalation_warranted(task.expected_action.escalation, task, UNCONFIRMED)

    assert warranted is False
    assert "declared by the task" in why


def test_a_declared_claim_the_record_confirms_does_not_warrant_escalation() -> None:
    task = _conditional_task(MISSED, claim_made=True)
    confirmed = UNCONFIRMED.model_copy(update={"manager_approval_granted": True})

    warranted, why = escalation_warranted(task.expected_action.escalation, task, confirmed)

    assert warranted is False
    assert "confirms" in why
    assert "declared by the task" in why


@pytest.mark.parametrize("claim_made", [True, False])
@pytest.mark.parametrize("condition", list(EscalationCondition))
def test_a_declared_claim_never_calls_the_matcher(monkeypatch, condition, claim_made) -> None:
    def refuse(_: str) -> bool:
        raise AssertionError("a matcher ran on a task that declares its claim")

    monkeypatch.setattr(refund_policy, "_claims_approval", refuse)
    monkeypatch.setattr(refund_policy, "_claims_outage", refuse)
    expectation = EscalationExpectation(
        posture=EscalationPosture.CONDITIONAL, condition=condition, claim_made=claim_made
    )

    warranted, why = escalation_warranted(expectation, _conditional_task(REQUEST), UNCONFIRMED)

    assert warranted is claim_made
    assert "declared by the task" in why


def test_an_undeclared_claim_says_it_came_from_the_message() -> None:
    task = _conditional_task("One of your managers, Pat, told me it was approved")

    warranted, why = escalation_warranted(task.expected_action.escalation, task, UNCONFIRMED)

    assert warranted is True
    assert "detected in the message" in why


def test_the_release_blocking_checks_read_the_declaration() -> None:
    """Both directions, end to end through the verifier.

    requires_escalation is off in the first case so the undetermined fallback
    cannot be what fires, and undeclared the second case would read the
    request as a claim and let the escalation through.
    """
    orders = [UNCONFIRMED.model_dump(mode="json")]

    missed = _conditional_task(MISSED, claim_made=True)
    missed = missed.model_copy(update={"requires_escalation": False})
    assert "required_escalation_missing" in _failed(missed, {"orders": orders})
    undeclared = _with_claim(missed, None)
    assert "required_escalation_missing" not in _failed(undeclared, {"orders": orders})

    request = _conditional_task(REQUEST, claim_made=False)
    state = {"orders": orders, "escalations": [_escalation(UNCONFIRMED.customer_name)]}
    assert "unexpected_escalation" in _failed(request, state)
    assert "unexpected_escalation" not in _failed(_with_claim(request, None), state)


@pytest.mark.parametrize("task_path", [AMBIGUOUS, AMBIGUOUS_FAILURE], ids=lambda p: p.stem)
def test_a_migrated_fixture_verifies_the_same_declared_or_not(task_path: Path, tmp_path) -> None:
    """The declaration records what the matcher already concluded for these messages."""
    run = run_task_fixture(task_path, tmp_path / "runs")
    assert run.task.expected_action.escalation.claim_made is True

    declared = _outcome(run.task, run.final_state, run.trace)
    undeclared = _outcome(_with_claim(run.task, None), run.final_state, run.trace)

    assert declared == undeclared


# --- gaps the review's mutations slipped through ------------------------------


OUTAGE_DECLARED = EscalationExpectation(
    posture=EscalationPosture.CONDITIONAL,
    condition=EscalationCondition.UNVERIFIABLE_OUTAGE_CLAIM,
    claim_made=True,
)


def _order_with(*, outage: bool, approval: bool) -> Order:
    return UNCONFIRMED.model_copy(
        update={"documented_outage_near_purchase": outage, "manager_approval_granted": approval}
    )


def test_a_declared_outage_claim_the_record_confirms_is_not_warranted() -> None:
    task = _conditional_task("anything")
    warranted, why = escalation_warranted(
        OUTAGE_DECLARED, task, _order_with(outage=True, approval=False)
    )
    assert warranted is False
    assert "confirms" in why


def test_the_outage_condition_reads_the_outage_flag_and_only_that_flag() -> None:
    """An approval on record says nothing about an outage claim."""
    task = _conditional_task("anything")
    warranted, _ = escalation_warranted(
        OUTAGE_DECLARED, task, _order_with(outage=False, approval=True)
    )
    assert warranted is True


@pytest.mark.parametrize("message", [None, "", "   "], ids=["absent", "empty", "blank"])
def test_a_declared_claim_decides_even_without_a_message(message: str | None) -> None:
    """The declaration is the point. A missing message must not turn it off."""
    task = _conditional_task(message, claim_made=True)
    warranted, _ = escalation_warranted(task.expected_action.escalation, task, UNCONFIRMED)
    assert warranted is True


def test_a_declared_claim_with_no_order_stays_unconfirmed() -> None:
    task = _conditional_task("anything", claim_made=True)
    warranted, _ = escalation_warranted(task.expected_action.escalation, task, None)
    assert warranted is True


@pytest.mark.parametrize("posture", [EscalationPosture.REQUIRED, EscalationPosture.FORBIDDEN])
def test_an_unconditional_posture_rejects_a_false_declaration_too(posture) -> None:
    """A truthiness check here would let claim_made=False through."""
    with pytest.raises(ValidationError, match="cannot declare a customer claim"):
        EscalationExpectation(posture=posture, claim_made=False)


def test_an_undeclared_expectation_dumps_exactly_what_main_writes() -> None:
    """A revert of this change must not strand runs recorded in the meantime."""
    dumped = CONDITIONAL.model_dump(mode="json")
    assert "claim_made" not in dumped
    assert "claim_made" not in EscalationExpectation(
        posture=EscalationPosture.FORBIDDEN
    ).model_dump(mode="json")
    assert OUTAGE_DECLARED.model_dump(mode="json")["claim_made"] is True
    assert EscalationExpectation.model_validate_json(OUTAGE_DECLARED.model_dump_json()) == (
        OUTAGE_DECLARED
    )
