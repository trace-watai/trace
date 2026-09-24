"""The exp_001 plan and its offline rehearsal (#200).

The plan at docs/acceptance/experiments/exp_001_replay_validity/experiment.json
must match pre-registration 001 section by section, and every condition must be
one ``branch`` accepts. Before the plan is frozen, each fork point must still
materialize exactly as retained, so a moved fixture or verifier is caught while
the plan can still change.

The rehearsal runs scripts/dry_run_exp_001.py, the pre-registration's harness
check over the whole plan with the fixture model standing in for every live
model. It must fill all eight metrics, agree on every pair with zero
divergence, and let scripts/regenerate_exp_001.sh reproduce result.json and the
B1 sidecar exactly. It runs in a scratch folder and is never retained.
"""

from __future__ import annotations

import pytest

from conftest import REPO_ROOT
from trace_harness.cli import main
from trace_harness.environment.controls import REFUND_WINDOW_CONTROL_ID
from trace_harness.models import is_priced
from trace_harness.runner.branch import load_artifact, replacement_seeds, validate_condition
from trace_harness.runner.experiment import ExperimentSpec

EXP_DIR = REPO_ROOT / "docs" / "acceptance" / "experiments" / "exp_001_replay_validity"
PLAN = ExperimentSpec.model_validate_json((EXP_DIR / "experiment.json").read_text())
# The pre-registration's Sample table: task, suite, family and control step.
REGISTERED = {
    "refund_policy_failure": ("both", "canonical", 5),
    "refund_cash_age_boundary_day_31_no_approval": ("refund_bundles_v0", "purchase_age", 4),
    "refund_cash_age_boundary_day_61_violation": ("refund_v0", "purchase_age", 3),
}
ARMS = ("static_replay", "live", "live_no_control", "live_swapped")


@pytest.fixture(autouse=True)
def _from_the_repository_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(REPO_ROOT)


def _fork_points() -> dict[str, dict]:
    return {fp["task_id"]: fp for fp in PLAN.metadata["fork_points"]}


# --- the plan against the pre-registration ---


def test_the_fork_points_are_the_three_registered_ones():
    fork_points = _fork_points()
    assert {
        task: (fp["suite"], fp["family"], fp["control_step"]) for task, fp in fork_points.items()
    } == REGISTERED
    for fp in fork_points.values():
        artifact = load_artifact(fp["artifact"])
        basis = artifact.replay_mode_basis
        assert artifact.source_run_id == fp["source_run_id"]
        assert (artifact.replay_mode, basis.control_step) == ("live_required", fp["control_step"])
        assert basis.first_irreversible_action_step == fp["control_step"]
        assert basis.control_ids == [REFUND_WINDOW_CONTROL_ID]
        assert [s.task_fixture for s in artifact.positive_sibling_tests] == fp["positive_siblings"]


def test_every_fork_point_runs_the_four_arms():
    by_task: dict[str, list[str]] = {}
    for condition in PLAN.conditions:
        arm, _, task = condition.name.partition("__")
        assert arm == condition.kind.value
        by_task.setdefault(task, []).append(arm)
    assert by_task == {task: list(ARMS) for task in REGISTERED}


def test_seeds_models_temperature_and_budget_follow_the_preregistration():
    assert PLAN.budget.max_cost_usd == 50.0
    assert replacement_seeds(PLAN) == [5, 6, 7, 8, 9]
    cassette_dirs = set()
    for condition in PLAN.conditions:
        agent = condition.agent_config
        if condition.kind.value == "static_replay":
            # Deterministic, so it runs once under the fixture adapter.
            assert (agent.provider, condition.seeds) == ("fixture", [])
            assert condition.control_ids == [REFUND_WINDOW_CONTROL_ID]
            continue
        assert condition.seeds == [0, 1, 2, 3, 4]
        assert agent.temperature is None, "the provider's default temperature"
        assert is_priced(agent.provider, agent.model), "an unpriced model cannot run under a cap"
        expected = {
            "live": ("gemini", "gemini-3.6-flash", [REFUND_WINDOW_CONTROL_ID]),
            "live_no_control": ("gemini", "gemini-3.6-flash", []),
            "live_swapped": ("anthropic", "claude-sonnet-5", [REFUND_WINDOW_CONTROL_ID]),
        }[condition.kind.value]
        assert (agent.provider, agent.model, condition.control_ids) == expected
        # Record mode, one folder per arm, since cassette paths do not name the arm.
        assert agent.cassette.mode == "record"
        assert agent.cassette.directory == str(
            EXP_DIR.relative_to(REPO_ROOT) / "cassettes" / condition.kind.value
        )
        cassette_dirs.add(agent.cassette.directory)
    assert len(cassette_dirs) == 3


def test_every_condition_is_one_branch_accepts():
    """The checks branch runs before any spend, run here on every condition."""
    fork_points = {fp["source_run_id"]: fp for fp in PLAN.metadata["fork_points"]}
    for condition in PLAN.conditions:
        fp = fork_points[condition.start.source_run_id]
        fork_step = validate_condition(load_artifact(fp["artifact"]), condition)
        expected = 0 if condition.kind.value == "static_replay" else fp["control_step"]
        assert fork_step == expected


@pytest.mark.skipif(
    PLAN.frozen_manifest.frozen_set is not None,
    reason="once frozen, the gate's frozen-set check guards the fork points",
)
@pytest.mark.parametrize("task", list(REGISTERED))
def test_each_fork_point_still_materializes_as_retained(tmp_path, task):
    fp = _fork_points()[task]
    retained = load_artifact(fp["artifact"])
    runs = tmp_path / "runs"
    assert main(["--runs-dir", str(runs), "run-pipeline", retained.task_fixture]) == 0
    (path,) = runs.glob("run_*/regression_artifact.json")
    fresh = load_artifact(path)
    fields = (
        "task_fixture",
        "initial_state",
        "pinned_docs",
        "pinned_agent_actions",
        "verifier_checks",
        "positive_sibling_tests",
        "replay_mode",
        "replay_mode_basis",
        "blocks_release",
    )
    for field in fields:
        assert getattr(fresh, field) == getattr(retained, field), field
