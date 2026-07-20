"""Structural validation tests for TaskSpec.

These cover the *schema-level* rules only — the universal structural contract.
Authoring-quality rules (non-empty tools/verifiers, describes behavior, no
clocks, multi-step) live in tasks/validation.py + the rubric and are tested
separately, so minimal stub tasks (e.g. in test_refund_verifier) stay valid.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from trace_harness.tasks.loader import load_task
from trace_harness.tasks.schemas import TaskSpec

REPO_ROOT = Path(__file__).resolve().parents[1]
TASKS_DIR = REPO_ROOT / "fixtures" / "tasks"
VALID_TASK_PATH = TASKS_DIR / "refund_policy_valid_cash.json"
FAILURE_TASK_PATH = TASKS_DIR / "refund_policy_failure.json"


def _valid_kwargs() -> dict:
    """Minimal kwargs that satisfy the structural schema."""
    return {
        "task_id": "unit_task",
        "title": "unit",
        "description": "unit",
        "goal": "unit",
        "workflow_type": "support.refund",
        "initial_state": {},
        "available_tools": ["get_order"],
    }


# --- the real fixtures must still load -------------------------------------


@pytest.mark.parametrize("path", [VALID_TASK_PATH, FAILURE_TASK_PATH])
def test_real_fixtures_load(path) -> None:
    task = load_task(path)
    assert task.task_id


# --- extra="forbid" --------------------------------------------------------


def test_unknown_field_is_rejected() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        TaskSpec(**_valid_kwargs(), typo_field=1)


# --- required text fields reject empty (min_length) ------------------------


@pytest.mark.parametrize("field", ["title", "description", "goal"])
def test_required_text_fields_reject_empty(field: str) -> None:
    with pytest.raises(ValidationError, match="at least 1 character"):
        TaskSpec(**{**_valid_kwargs(), field: ""})


# --- task_id slug pattern --------------------------------------------------


@pytest.mark.parametrize("bad_id", ["Refund_Task", "refund task", "-leading", "réfund"])
def test_task_id_must_be_a_slug(bad_id: str) -> None:
    with pytest.raises(ValidationError, match="should match pattern"):
        TaskSpec(**{**_valid_kwargs(), "task_id": bad_id})


# --- workflow_type pattern -------------------------------------------------


@pytest.mark.parametrize("bad_wf", ["Support.Refund", "support refund", "support."])
def test_workflow_type_must_be_dotted_lowercase(bad_wf: str) -> None:
    with pytest.raises(ValidationError, match="should match pattern"):
        TaskSpec(**{**_valid_kwargs(), "workflow_type": bad_wf})


# --- schema_version semver -------------------------------------------------


def test_schema_version_must_be_semver() -> None:
    with pytest.raises(ValidationError, match="should match pattern"):
        TaskSpec(**{**_valid_kwargs(), "schema_version": "v1"})


# --- uniqueness (all four list fields) -------------------------------------


@pytest.mark.parametrize(
    "field", ["available_tools", "available_docs", "verifier_ids", "targeted_failure_modes"]
)
def test_duplicate_list_entries_rejected(field: str) -> None:
    with pytest.raises(ValidationError, match="duplicate entries"):
        TaskSpec(**{**_valid_kwargs(), field: ["dup", "dup"]})


# --- difficulty (optional, enum-validated) ---------------------------------


def test_difficulty_is_optional_and_validated() -> None:
    assert TaskSpec(**_valid_kwargs()).difficulty is None
    assert TaskSpec(**{**_valid_kwargs(), "difficulty": "hard"}).difficulty == "hard"
    with pytest.raises(ValidationError, match="Input should be"):
        TaskSpec(**{**_valid_kwargs(), "difficulty": "trivial"})


# --- requires_escalation (optional bool, defaults false) -------------------


def test_requires_escalation_defaults_false_and_round_trips() -> None:
    assert TaskSpec(**_valid_kwargs()).requires_escalation is False
    task = TaskSpec(**{**_valid_kwargs(), "requires_escalation": True})
    assert task.requires_escalation is True
    # survives a JSON round-trip so it reaches the verifier via the run artifact
    assert TaskSpec.model_validate(task.model_dump()).requires_escalation is True


# --- docs resolvability ----------------------------------------------------


def test_available_docs_without_source_is_rejected() -> None:
    with pytest.raises(ValidationError, match="cannot be resolved"):
        TaskSpec(**{**_valid_kwargs(), "available_docs": ["policy_v4"]})


def test_available_docs_with_inline_docs_ok() -> None:
    task = TaskSpec(
        **{
            **_valid_kwargs(),
            "available_docs": ["policy_v4"],
            "initial_state": {"docs": [{"doc_id": "policy_v4"}]},
        }
    )
    assert task.available_docs == ["policy_v4"]


# --- leniency the schema must preserve (so stubs keep working) -------------


def test_empty_content_lists_are_structurally_valid() -> None:
    """available_tools=[] and no verifier/failure-mode lists must parse — those
    are authoring-quality concerns, enforced by validate-fixtures, not here."""
    task = TaskSpec(**{**_valid_kwargs(), "available_tools": []})
    assert task.verifier_ids == []
    assert task.targeted_failure_modes == []
