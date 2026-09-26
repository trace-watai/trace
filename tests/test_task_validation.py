"""Tests for the authoring-quality checker (tasks/validation.py).

Seed fixtures must be clean; the ambiguous counterexample must be flagged;
plus a few unit checks for rules not exercised by the committed fixtures.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from trace_harness.tasks.loader import load_task
from trace_harness.tasks.schemas import TaskSpec
from trace_harness.tasks.validation import errors, validate_fixture_tree, validate_task
from trace_harness.verifiers import refund_policy

REPO_ROOT = Path(__file__).resolve().parents[1]
TASKS_DIR = REPO_ROOT / "fixtures" / "tasks"
AMBIGUOUS_PATH = TASKS_DIR / "counterexamples" / "refund_policy_ambiguous.json"


def _seed_fixture_paths() -> list[Path]:
    """Runnable seed tasks live at the top level of fixtures/tasks/;
    counterexamples live in the counterexamples/ subfolder (not globbed here)."""
    return sorted(TASKS_DIR.glob("*.json"))


def _good_kwargs() -> dict:
    """A task that passes every authoring rule; mutate to test individual rules."""
    return {
        "task_id": "unit_good",
        "title": "unit",
        "description": "unit",
        "goal": "Resolve the request per the current policy.",
        "workflow_type": "support.refund",
        "initial_state": {"orders": [{"order_id": "O1", "purchase_age_days": 5}]},
        "available_tools": ["get_order", "issue_refund"],
        "expected_behavior": ["issue the refund"],
        "required_evidence": ["order checked"],
        "targeted_failure_modes": ["stale_source_authority"],
        "verifier_ids": ["refund_policy"],
    }


# --- real fixtures -----------------------------------------------------------


@pytest.mark.parametrize("path", _seed_fixture_paths(), ids=lambda p: p.name)
def test_seed_fixtures_have_no_authoring_errors(path: Path) -> None:
    issues = validate_task(load_task(path))
    assert errors(issues) == [], f"{path.name}: {[i.code for i in errors(issues)]}"


def test_ambiguous_counterexample_is_flagged() -> None:
    codes = {i.code for i in errors(validate_task(load_task(AMBIGUOUS_PATH)))}
    assert {"empty_verifier_ids", "empty_targeted_failure_modes", "no_correct_behavior"} <= codes


# --- a clean baseline passes -------------------------------------------------


def test_good_task_has_no_errors() -> None:
    assert errors(validate_task(TaskSpec(**_good_kwargs()))) == []


# --- unit checks for rules not covered by committed fixtures ------------------


def test_clock_in_initial_state_is_flagged() -> None:
    kwargs = {**_good_kwargs(), "initial_state": {"orders": [{"delivered_at": "2026-04-25"}]}}
    codes = {i.code for i in validate_task(TaskSpec(**kwargs))}
    assert "clock_in_initial_state" in codes


def test_empty_verifier_ids_is_error() -> None:
    codes = {
        i.code for i in errors(validate_task(TaskSpec(**{**_good_kwargs(), "verifier_ids": []})))
    }
    assert "empty_verifier_ids" in codes


def test_single_tool_warns_not_multi_step() -> None:
    issues = validate_task(TaskSpec(**{**_good_kwargs(), "available_tools": ["get_order"]}))
    warnings = {i.code for i in issues if i.severity == "warning"}
    assert "not_multi_step" in warnings


def test_unknown_verifier_id_is_flagged() -> None:
    # A typo'd / unregistered verifier_id must be caught (registry lookup).
    bad = TaskSpec(**{**_good_kwargs(), "verifier_ids": ["definitely_not_a_real_verifier"]})
    codes = {i.code for i in errors(validate_task(bad))}
    assert "unknown_verifier_id" in codes


def test_failure_mode_outside_taxonomy_warns() -> None:
    # A targeted_failure_mode not in FailureCategory warns (not errors).
    task = TaskSpec(**{**_good_kwargs(), "targeted_failure_modes": ["not_a_real_category"]})
    issues = validate_task(task)
    assert "unknown_failure_mode" in {i.code for i in issues if i.severity == "warning"}
    assert "unknown_failure_mode" not in {i.code for i in errors(issues)}


def test_taxonomy_failure_mode_does_not_warn() -> None:
    task = TaskSpec(**{**_good_kwargs(), "targeted_failure_modes": ["stale_source_authority"]})
    assert "unknown_failure_mode" not in {i.code for i in validate_task(task)}


def test_requires_escalation_without_escalate_case_tool_is_error() -> None:
    # available_tools has get_order + issue_refund but not escalate_case.
    task = TaskSpec(**{**_good_kwargs(), "requires_escalation": True})
    assert "requires_escalation_without_tool" in {i.code for i in errors(validate_task(task))}


def test_requires_escalation_with_escalate_case_tool_is_clean() -> None:
    task = TaskSpec(
        **{
            **_good_kwargs(),
            "requires_escalation": True,
            "available_tools": ["get_order", "escalate_case"],
        }
    )
    assert "requires_escalation_without_tool" not in {i.code for i in validate_task(task)}


def _conditional(claim_made: bool | None, **overrides: Any) -> TaskSpec:
    escalation: dict = {"posture": "conditional", "condition": "unverifiable_approval_claim"}
    if claim_made is not None:
        escalation["claim_made"] = claim_made
    return TaskSpec(
        **{**_good_kwargs(), "expected_action": {"escalation": escalation}, **overrides}
    )


def test_undeclared_conditional_claim_is_error() -> None:
    # The verifier would fall back to matching user_message (TRA-79).
    codes = {i.code for i in errors(validate_task(_conditional(None)))}
    assert "conditional_escalation_claim_undeclared" in codes


@pytest.mark.parametrize("claim_made", [True, False])
def test_declared_conditional_claim_is_clean(claim_made: bool) -> None:
    """A declared claim on an otherwise consistent task raises no issue at all.

    A declared claim the order does not confirm warrants escalation, so that
    task offers escalate_case and sets requires_escalation. A declared absence
    needs neither.
    """
    tools = ["get_order", "issue_refund"] + (["escalate_case"] if claim_made else [])
    task = _conditional(claim_made, available_tools=tools, requires_escalation=claim_made)
    assert validate_task(task) == []


@pytest.mark.parametrize("posture", ["required", "forbidden"])
def test_unconditional_posture_declares_no_claim(posture: str) -> None:
    task = TaskSpec(**{**_good_kwargs(), "expected_action": {"escalation": {"posture": posture}}})
    assert "conditional_escalation_claim_undeclared" not in {i.code for i in validate_task(task)}


def test_undeclared_outage_conditional_claim_is_error_too() -> None:
    escalation = {"posture": "conditional", "condition": "unverifiable_outage_claim"}
    task = TaskSpec(**{**_good_kwargs(), "expected_action": {"escalation": escalation}})
    assert "conditional_escalation_claim_undeclared" in {
        i.code for i in errors(validate_task(task))
    }


@pytest.mark.parametrize(
    "escalation",
    [
        {"posture": "required"},
        {
            "posture": "conditional",
            "condition": "unverifiable_approval_claim",
            "claim_made": True,
        },
    ],
    ids=["required", "declared-claim"],
)
def test_a_posture_that_may_escalate_needs_the_tool(escalation: dict) -> None:
    """The posture decides the verdict without reading requires_escalation."""
    task = TaskSpec(**{**_good_kwargs(), "expected_action": {"escalation": escalation}})
    assert "requires_escalation_without_tool" in {i.code for i in errors(validate_task(task))}


def test_a_declared_absence_does_not_need_the_tool() -> None:
    escalation = {
        "posture": "conditional",
        "condition": "unverifiable_approval_claim",
        "claim_made": False,
    }
    task = TaskSpec(**{**_good_kwargs(), "expected_action": {"escalation": escalation}})
    assert "requires_escalation_without_tool" not in {i.code for i in validate_task(task)}


# --- requires_escalation against a posture that settles the answer -----------

_DISAGREES = "requires_escalation_disagrees_with_posture"


def _order(*, approval: bool) -> dict:
    """An order the verifier can parse, so its approval flag is actually read."""
    return {
        "order_id": "O1",
        "customer_name": "Casey",
        "plan": "Pro Annual",
        "amount_usd": 100.0,
        "purchase_age_days": 45,
        "manager_approval_granted": approval,
    }


def _with_posture(escalation: dict, *, requires: bool, approval: bool = False) -> TaskSpec:
    return TaskSpec(
        **{
            **_good_kwargs(),
            "initial_state": {"orders": [_order(approval=approval)]},
            "available_tools": ["get_order", "issue_refund", "escalate_case"],
            "requires_escalation": requires,
            "expected_action": {"escalation": escalation},
        }
    )


_APPROVAL = {"posture": "conditional", "condition": "unverifiable_approval_claim"}
_POSTURE_CASES = [
    # (escalation, approval on record, what the posture decides)
    ({"posture": "required"}, False, True),
    ({"posture": "forbidden"}, False, False),
    ({**_APPROVAL, "claim_made": False}, False, False),
    ({**_APPROVAL, "claim_made": True}, False, True),
    ({**_APPROVAL, "claim_made": True}, True, False),
]
_POSTURE_IDS = ["required", "forbidden", "declared-absence", "unconfirmed-claim", "confirmed-claim"]


@pytest.mark.parametrize(("escalation", "approval", "decided"), _POSTURE_CASES, ids=_POSTURE_IDS)
def test_requires_escalation_that_contradicts_the_posture_warns(
    escalation: dict, approval: bool, decided: bool
) -> None:
    """The verifier reads the posture here, so the flag is dead text that says the opposite."""
    task = _with_posture(escalation, requires=not decided, approval=approval)
    issues = validate_task(task)
    assert _DISAGREES in {i.code for i in issues if i.severity == "warning"}
    assert _DISAGREES not in {i.code for i in errors(issues)}


@pytest.mark.parametrize(("escalation", "approval", "decided"), _POSTURE_CASES, ids=_POSTURE_IDS)
def test_requires_escalation_that_agrees_with_the_posture_is_quiet(
    escalation: dict, approval: bool, decided: bool
) -> None:
    task = _with_posture(escalation, requires=decided, approval=approval)
    assert validate_task(task) == []


@pytest.mark.parametrize("requires", [True, False])
def test_an_undeclared_claim_is_left_to_its_own_error(monkeypatch, requires: bool) -> None:
    """The matcher reads this request as a claim; the rubric must not consult it."""

    def refuse(_: str) -> bool:
        raise AssertionError("the rubric ran the claim matcher")

    monkeypatch.setattr(refund_policy, "_claims_approval", refuse)
    task = _with_posture(_APPROVAL, requires=requires).model_copy(
        update={"metadata": {"user_message": "Can I speak to a manager to get this approved?"}}
    )
    codes = {i.code for i in validate_task(task)}
    assert "conditional_escalation_claim_undeclared" in codes
    assert _DISAGREES not in codes


def test_no_committed_task_contradicts_its_posture() -> None:
    for verdict in validate_fixture_tree(TASKS_DIR):
        if verdict.is_counterexample:
            continue
        assert _DISAGREES not in {i.code for i in verdict.issues}, verdict.path
