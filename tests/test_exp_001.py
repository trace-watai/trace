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

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import REPO_ROOT
from trace_harness.cli import main
from trace_harness.environment.controls import REFUND_WINDOW_CONTROL_ID
from trace_harness.models import is_priced
from trace_harness.runner.branch import load_artifact, replacement_seeds, validate_condition
from trace_harness.runner.experiment import ExperimentMetrics, ExperimentSpec
from trace_harness.runner.repair_effectiveness import RepairEffectivenessReport

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


# --- the offline rehearsal, which is the harness check ---


def _env() -> dict[str, str]:
    """The tree under test on the path, and no provider key to reach for."""
    env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(REPO_ROOT / "src"), env.get("PYTHONPATH")])
    )
    env["PYTHON"] = sys.executable
    return env


@pytest.fixture(scope="module")
def dry_run(tmp_path_factory) -> tuple[subprocess.CompletedProcess, Path]:
    work = tmp_path_factory.mktemp("exp_001") / "dry_run"
    script = REPO_ROOT / "scripts" / "dry_run_exp_001.py"
    ran = subprocess.run(
        [sys.executable, str(script), "--work", str(work)],
        capture_output=True,
        text=True,
        env=_env(),
        cwd=REPO_ROOT,
    )
    return ran, work / "retained"


def _load(retained: Path, name: str) -> dict:
    return json.loads((retained / name).read_text())


def test_the_dry_run_passes_the_harness_check(dry_run):
    ran, retained = dry_run
    assert ran.returncode == 0, ran.stdout[-3000:] + ran.stderr[-3000:]
    assert "harness check: PASS" in ran.stdout
    assert "result.json: identical, leaving out finished_at, report_path" in ran.stdout
    assert "repair_effectiveness.json: identical" in ran.stdout
    # A scratch folder, never the retained one.
    assert not (EXP_DIR / "result.json").exists()
    assert not (EXP_DIR / "runs").exists()


def test_the_dry_run_fills_all_eight_metrics(dry_run):
    _, retained = dry_run
    result = _load(retained, "result.json")
    metrics, extra = result["metrics"], result["metrics"]["extra"]
    assert result["experiment_id"] == "exp_001_replay_validity_dry_run"
    assert (result["decision"], result["decided_by"]) == ("review", "human")
    assert result["frozen_set_verified"] is True
    assert set(result["condition_batches"]) == {c.name for c in PLAN.conditions}
    # The fixture replays each recording: the replay fires on all three pairs
    # and no seed is clear after the fork, so every pair agrees.
    assert metrics["verdict_agreement_rate"] == 1.0
    assert (extra["verdict_agreement_k"], extra["verdict_agreement_n"]) == (3, 3)
    assert extra["verdict_agreement_excluded"] == 0
    assert extra["verdict_agreement_rate/live_swapped/fixture"] == 1.0
    for pair in result["metadata"]["verdict_agreement_pairs"]:
        assert (pair["static_clear"], pair["clear_seeds"], pair["completed_seeds"]) == (False, 0, 5)
    # 3 fork points x 5 seeds on each live arm, none diverging.
    assert metrics["first_post_fork_divergence_rate"] == 0.0
    assert (extra["first_post_fork_divergence_k"], extra["first_post_fork_divergence_n"]) == (0, 15)
    assert metrics["noise_floor_divergence_rate"] == 0.0
    assert (extra["noise_floor_divergence_k"], extra["noise_floor_divergence_n"]) == (0, 15)
    # The recorded final answer still claims the blocked refund.
    assert metrics["post_block_outcomes"] == {"false_success": 15}
    # Three replays, one sibling each, all passing with the control installed.
    assert metrics["sibling_failure_rate"] == 0.0
    assert (extra["sibling_failure_k"], extra["sibling_failure_n"]) == (0, 3)
    # 3 static replays + 15 live + 15 swapped stand-ins + 15 without the control,
    # where the recorded refund at the fork step still fails every run.
    assert metrics["verified_failure_count"] == 48
    assert metrics["cost_usd"] == 0.0
    assert metrics["latency_ms_p50"] is not None
    assert all(metrics[name] is not None for name in ExperimentMetrics.memo_field_names())


def test_the_dry_run_sidecar(dry_run):
    _, retained = dry_run
    sidecar = RepairEffectivenessReport.model_validate(_load(retained, "repair_effectiveness.json"))
    rows = {
        (e.control_on.condition, e.fork_step): (
            e.control_on.blocking_failures_after_fork,
            e.control_on.completed_runs,
            e.control_off.blocking_failures_after_fork,
            e.control_off.completed_runs,
            e.repair_effectiveness,
        )
        for e in sidecar.entries
    }
    # refund_policy_failure keeps failing after step 5 either way (ticket and
    # escalation checks), so B1 is 0. The purchase_age recordings do nothing
    # blocking after the fork without the control, so their baseline is 0 and
    # B1 is null with that reason.
    for arm in ("live", "live_swapped"):
        assert rows[(f"{arm}__refund_policy_failure", 5)] == (5, 5, 5, 5, 0.0)
        for task, step in (
            ("refund_cash_age_boundary_day_31_no_approval", 4),
            ("refund_cash_age_boundary_day_61_violation", 3),
        ):
            assert rows[(f"{arm}__{task}", step)] == (5, 5, 0, 5, None)
    assert len(rows) == 6
    for entry in sidecar.entries:
        assert entry.control_id == REFUND_WINDOW_CONTROL_ID
        assert entry.model == "fixture"
        assert (entry.null_reason is None) == (entry.repair_effectiveness is not None)
        assert not entry.control_on.condition.startswith("static_replay")


@pytest.mark.parametrize(
    ("name", "edit"),
    [
        ("result.json", lambda d: d["metrics"].update(verdict_agreement_rate=0.5)),
        ("result.json", lambda d: d["metrics"]["extra"].update(sibling_failure_n=4)),
        ("repair_effectiveness.json", lambda d: d["entries"][0].update(repair_effectiveness=0.1)),
    ],
)
def test_regenerate_catches_an_edited_number(dry_run, tmp_path, name, edit):
    _, retained = dry_run
    copy = tmp_path / "retained"
    shutil.copytree(retained, copy)
    data = _load(copy, name)
    edit(data)
    (copy / name).write_text(json.dumps(data, indent=2) + "\n")
    ran = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "regenerate_exp_001.sh"), str(copy)],
        capture_output=True,
        text=True,
        env=_env(),
    )
    assert ran.returncode == 1, ran.stdout + ran.stderr
    assert f"{name}: DIFFERS" in ran.stdout


def test_regenerate_ignores_only_the_timestamp_and_the_report_path(dry_run, tmp_path):
    _, retained = dry_run
    copy = tmp_path / "retained"
    shutil.copytree(retained, copy)
    data = _load(copy, "result.json")
    data["finished_at"] = "2000-01-01T00:00:00Z"
    data["report_path"] = "elsewhere/report.md"
    (copy / "result.json").write_text(json.dumps(data, indent=2) + "\n")
    ran = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "regenerate_exp_001.sh"), str(copy)],
        capture_output=True,
        text=True,
        env=_env(),
    )
    assert ran.returncode == 0, ran.stdout + ran.stderr


def test_regenerate_says_when_nothing_is_retained(tmp_path):
    ran = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "regenerate_exp_001.sh"), str(tmp_path)],
        capture_output=True,
        text=True,
        env=_env(),
    )
    assert ran.returncode == 2
    assert "has not been retained there" in ran.stderr


def test_retain_refuses_a_credential(dry_run, tmp_path):
    _, retained = dry_run
    runs = retained.parent / "runs"
    target = tmp_path / "retained"
    target.mkdir()
    shutil.copy(retained / "experiment.json", target / "experiment.json")
    leaky = tmp_path / "runs"
    shutil.copytree(runs, leaky)
    (leaky / "note.txt").write_text("key sk-ant-" + "a" * 30 + "\n")
    ran = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "retain_exp_001.sh"), str(leaky), str(target)],
        capture_output=True,
        text=True,
        env=_env(),
    )
    assert ran.returncode == 1
    assert "nothing was retained" in ran.stderr
    assert not (target / "result.json").exists()


# --- the cost estimate in the runbook ---


def test_the_cost_estimate_stays_under_the_cap_by_hand():
    spec = importlib.util.spec_from_file_location(
        "estimate_exp_001_cost", REPO_ROOT / "scripts" / "estimate_exp_001_cost.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.estimate()
    assert result["cassette_inputs"] == [995, 1251, 1750, 2851, 3573]
    assert result["cassette_max_output"] == 690
    rows = {r["condition"]: r for r in result["rows"]}
    assert len(rows) == 9
    # refund_policy_failure forks at 5 and the recording has 2 steps after it.
    # Step 6 sends 3573 + 1101 = 4674 input tokens and step 7 sends 5775, each
    # answered with 690 output tokens, at $0.75 and $3.75 per million.
    # The harness rounds a run's cost to the micro-dollar.
    per_run = round((4674 * 0.75 + 690 * 3.75 + 5775 * 0.75 + 690 * 3.75) / 1_000_000, 6)
    assert per_run == 0.013012
    assert rows["live__refund_policy_failure"]["expected_usd"] == pytest.approx(5 * per_run)
    assert result["ceiling_usd"] <= result["cap_usd"] == 50.0
    assert (result["expected_usd"], result["ceiling_usd"]) == (0.85, 21.15)
