"""Tests for the authoring-quality checker (tasks/validation.py).

Seed fixtures must be clean; the ambiguous counterexample must be flagged;
plus a few unit checks for rules not exercised by the committed fixtures.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trace_harness.tasks.loader import load_task
from trace_harness.tasks.schemas import TaskSpec
from trace_harness.tasks.validation import errors, validate_task

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
