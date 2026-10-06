"""What a plan must say before it is accepted (#155).

A plan is written by hand before anything runs, so every mistake it can carry
has to fail when it loads. A misspelled control id that loads cleanly is found
only after the sweep has spent its budget, and an experiment id is a directory
name, so a hand-written one must not be able to point outside the runs
directory.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
from pydantic import ValidationError

from conftest import REPO_ROOT
from trace_harness.environment.control_library import ControlLibrary
from trace_harness.environment.controls import GUARDRAIL_REGISTRY, REFUND_WINDOW_CONTROL_ID
from trace_harness.runner.experiment import (
    ConditionKind,
    ConditionSpec,
    Decision,
    ExperimentResult,
    ExperimentSpec,
    new_experiment_id,
)
from trace_harness.runner.suite import AgentConfig
from trace_harness.tracing.artifact_store import ArtifactStore


def _condition(**overrides) -> dict:
    base = {
        "name": "live_on",
        "kind": ConditionKind.LIVE.value,
        "agent_config": AgentConfig(label="fixture-baseline").model_dump(mode="json"),
    }
    return {**base, **overrides}


def _plan(**overrides) -> dict:
    base = {
        "experiment_id": "exp_20260101T000000Z_baseline",
        "hypothesis": "the refund window control stops the out-of-window cash refund",
        "frozen_manifest": {
            "suite_id": "refund_bundles_v0",
            "verifier_ids": ["refund_policy"],
            "fixtures_hash": "sha256:deadbeef",
        },
        "conditions": [_condition()],
        "budget": {"max_runs": 10, "max_cost_usd": 0.0},
        "created_at": "2026-01-01T00:00:00Z",
    }
    return {**base, **overrides}


# --- control ids ---


def test_an_unknown_control_id_fails_when_the_plan_loads() -> None:
    plan = _plan(conditions=[_condition(control_ids=["ctl_refund_windw_v1"])])
    with pytest.raises(ValidationError, match="unknown control id"):
        ExperimentSpec.model_validate(plan)


def test_a_registered_control_id_loads() -> None:
    plan = _plan(conditions=[_condition(control_ids=[REFUND_WINDOW_CONTROL_ID])])
    spec = ExperimentSpec.model_validate(plan)
    assert spec.conditions[0].control_ids == [REFUND_WINDOW_CONTROL_ID]


def test_a_control_whose_guardrail_left_the_registry_fails(monkeypatch) -> None:
    """The id alone is not enough. Its guardrail must still be registered."""
    monkeypatch.delitem(GUARDRAIL_REGISTRY, "unauthorized_cash_refund_guardrail")
    with pytest.raises(ValidationError, match="unknown guardrail_ref"):
        ConditionSpec.model_validate(_condition(control_ids=[REFUND_WINDOW_CONTROL_ID]))


def test_a_control_id_listed_twice_is_rejected() -> None:
    """Installing one control twice raises at install time, so refuse it here."""
    ids = [REFUND_WINDOW_CONTROL_ID, REFUND_WINDOW_CONTROL_ID]
    with pytest.raises(ValidationError, match="more than once"):
        ConditionSpec.model_validate(_condition(control_ids=ids))


def test_every_control_in_the_library_can_be_named_by_a_plan() -> None:
    """The committed library and the plan validator agree on what exists."""
    library = ControlLibrary.model_validate_json(
        (REPO_ROOT / "fixtures" / "controls" / "library.json").read_bytes()
    )
    assert library.entries
    for entry in library.entries:
        ConditionSpec.model_validate(_condition(control_ids=[entry.control.control_id]))


@pytest.mark.parametrize(
    "module",
    [
        "trace_harness.runner.experiment",
        "trace_harness.environment.controls",
        "trace_harness.run_reader",
        "trace_harness.cli",
    ],
)
def test_the_plan_model_imports_first_without_a_cycle(module: str) -> None:
    """Checking control ids made the plan model import the control registry.

    Each module is imported first in a fresh interpreter, since an import
    cycle only shows up for the module that starts it.
    """
    done = subprocess.run(
        [sys.executable, "-c", f"import {module}"], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr


# --- experiment ids ---


@pytest.mark.parametrize(
    "experiment_id", ["../../escaped", "exp/inner", "/tmp/exp", "..", ".", "exp.x", "", "exp\\x"]
)
def test_an_experiment_id_that_is_not_one_path_segment_is_rejected(experiment_id: str) -> None:
    with pytest.raises(ValidationError):
        ExperimentSpec.model_validate(_plan(experiment_id=experiment_id))
    with pytest.raises(ValidationError):
        ExperimentResult(experiment_id=experiment_id, decision=Decision.REVIEW, decided_by="human")


@pytest.mark.parametrize("experiment_id", ["../x", "a/b", "..", ".", "", "a\\b", "/abs"])
def test_the_store_refuses_to_build_a_path_from_a_bad_id(tmp_path, experiment_id: str) -> None:
    with pytest.raises(ValueError, match="not a valid experiment id"):
        ArtifactStore(tmp_path).experiment_dir(experiment_id)


def test_generated_and_retained_ids_fit_the_pattern() -> None:
    ExperimentSpec.model_validate(_plan(experiment_id=new_experiment_id()))
    retained = REPO_ROOT / "docs" / "acceptance" / "experiments"
    for plan in retained.glob("*/experiment.json"):
        ExperimentSpec.model_validate(json.loads(plan.read_text(encoding="utf-8")))
