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
B1 sidecar exactly. It runs on a frozen copy of the plan, the state runbook
step 3 runs it in, in a scratch folder, and never touches the retained one.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

import pytest

from conftest import REPO_ROOT
from trace_harness.cli import main
from trace_harness.environment.controls import REFUND_WINDOW_CONTROL_ID
from trace_harness.models import is_priced
from trace_harness.models.anthropic import ANTHROPIC_PRICING
from trace_harness.models.gemini import GEMINI_PRICING
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
        # The provider's default, and claude-sonnet-5 rejects any other with a 400.
        assert agent.temperature is None
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


class Rehearsal(NamedTuple):
    ran: subprocess.CompletedProcess
    retained: Path
    plan: Path
    experiment_dir_before: dict[str, str]
    experiment_dir_after: dict[str, str]


def _digests(folder: Path) -> dict[str, str]:
    return {
        str(path.relative_to(folder)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(folder.rglob("*"))
        if path.is_file()
    }


def _dry_run(plan: Path, work: Path, *extra: str) -> subprocess.CompletedProcess:
    script = REPO_ROOT / "scripts" / "dry_run_exp_001.py"
    return subprocess.run(
        [sys.executable, str(script), "--plan", str(plan), "--work", str(work), *extra],
        capture_output=True,
        text=True,
        env=_env(),
        cwd=REPO_ROOT,
    )


@pytest.fixture(scope="module")
def frozen_plan(tmp_path_factory) -> Path:
    """A frozen copy of the committed plan, which is what runbook step 3 rehearses.

    Once the committed plan is frozen, this is a plain copy of it.
    """
    plan = tmp_path_factory.mktemp("exp_001_plan") / "experiment.json"
    shutil.copy(EXP_DIR / "experiment.json", plan)
    if PLAN.frozen_manifest.frozen_set is None:
        with contextlib.chdir(REPO_ROOT), contextlib.redirect_stdout(io.StringIO()):
            assert main(["experiment", "freeze", str(plan)]) == 0
    return plan


@pytest.fixture(scope="module")
def dry_run(tmp_path_factory, frozen_plan) -> Rehearsal:
    work = tmp_path_factory.mktemp("exp_001") / "dry_run"
    before = _digests(EXP_DIR)
    ran = _dry_run(frozen_plan, work)
    return Rehearsal(ran, work / "retained", frozen_plan, before, _digests(EXP_DIR))


def _load(retained: Path, name: str) -> dict:
    return json.loads((retained / name).read_text())


def test_the_dry_run_passes_the_harness_check(dry_run):
    ran = dry_run.ran
    assert ran.returncode == 0, ran.stdout[-3000:] + ran.stderr[-3000:]
    assert "harness check: PASS" in ran.stdout
    assert "result.json: identical, leaving out finished_at, report_path" in ran.stdout
    assert "repair_effectiveness.json: identical" in ran.stdout
    # A scratch folder, never the retained one, whatever step 8 has committed there.
    assert dry_run.experiment_dir_after == dry_run.experiment_dir_before
    assert (dry_run.retained / "result.json").is_file()


def test_the_harness_check_is_retained_beside_the_frozen_plan(dry_run):
    """Runbook step 3 leaves harness_check.json beside the plan it rehearsed."""
    record = json.loads((dry_run.plan.parent / "harness_check.json").read_text())
    plan = ExperimentSpec.model_validate_json(dry_run.plan.read_text())
    assert (record["passed"], record["problems"]) == (True, [])
    assert record["experiment_id"] == PLAN.experiment_id
    assert record["plan_sha256"] == hashlib.sha256(dry_run.plan.read_bytes()).hexdigest()
    assert record["frozen_set"] == {
        name: component.digest for name, component in plan.frozen_manifest.frozen_set.items()
    }
    result = _load(dry_run.retained, "result.json")
    assert record["metrics"] == result["metrics"]
    assert record["verdict_agreement_pairs"] == result["metadata"]["verdict_agreement_pairs"]
    sidecar = _load(dry_run.retained, "repair_effectiveness.json")
    assert record["repair_effectiveness"] == sidecar["entries"]
    assert f"harness check: retained in {dry_run.plan.parent / 'harness_check.json'}" in (
        dry_run.ran.stdout
    )


@pytest.mark.skipif(
    not (EXP_DIR / "harness_check.json").exists(), reason="runbook step 3 has not run yet"
)
def test_the_retained_harness_check_passed_on_the_committed_plan():
    record = json.loads((EXP_DIR / "harness_check.json").read_text())
    assert record["passed"] is True, record["problems"]
    plan_bytes = (EXP_DIR / "experiment.json").read_bytes()
    assert record["plan_sha256"] == hashlib.sha256(plan_bytes).hexdigest()


def test_an_unfrozen_plan_is_rehearsed_and_nothing_is_retained(tmp_path):
    """Before step 2 the dry run freezes its own stand-in, and its check is a preview."""
    raw = json.loads((EXP_DIR / "experiment.json").read_text())
    raw["frozen_manifest"]["frozen_set"] = None
    raw["frozen_manifest"]["fixtures_hash"] = "sha256:set-by-experiment-freeze"
    plan = tmp_path / "plan" / "experiment.json"
    plan.parent.mkdir()
    plan.write_text(json.dumps(raw, indent=2) + "\n")

    ran = _dry_run(plan, tmp_path / "work")

    assert ran.returncode == 0, ran.stdout[-3000:] + ran.stderr[-3000:]
    assert "harness check: PASS" in ran.stdout
    assert "harness check: not retained, since the plan is not frozen" in ran.stdout
    assert sorted(p.name for p in plan.parent.iterdir()) == ["experiment.json"]


def test_the_dry_run_fills_all_eight_metrics(dry_run):
    retained = dry_run.retained
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
    retained = dry_run.retained
    sidecar = RepairEffectivenessReport.model_validate(_load(retained, "repair_effectiveness.json"))
    assert {(e.arm, e.control_on.condition.split("__")[0]) for e in sidecar.entries} == {
        ("live", "live"),
        ("live_swapped", "live_swapped"),
    }
    # The fixture answers both arms, so only the arm tells their report rows apart.
    report = (retained / "report.md").read_text()
    assert "| artifact | arm | control | model | control on | control off | B1 |" in report
    for arm in ("live", "live_swapped"):
        assert report.count(f"| {arm} | {REFUND_WINDOW_CONTROL_ID} | fixture |") == 3
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
    # Both arms replay the recorded refund at the fork step, and B1 counts only
    # checks after it, so the control-off refund at the fork step never counts
    # (runbook_001.md, "B1 at the registered fork points"). refund_policy_failure
    # also fails at steps 6 and 7 either way, so its B1 is 0. The purchase_age
    # recordings fail only at the fork step without the control, so their
    # baseline is 0 and B1 is null with that reason.
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
    retained = dry_run.retained
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
    retained = dry_run.retained
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


def _dry_run_module():
    spec = importlib.util.spec_from_file_location(
        "dry_run_exp_001", REPO_ROOT / "scripts" / "dry_run_exp_001.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_harness_check_fails_a_disagreement_a_divergence_or_a_null(dry_run):
    retained = dry_run.retained
    check = _dry_run_module().harness_check
    result = _load(retained, "result.json")
    assert check(result) == []

    disagreeing = json.loads(json.dumps(result))
    disagreeing["metadata"]["verdict_agreement_pairs"][0]["agrees"] = False
    assert len(check(disagreeing)) == 1
    diverging = json.loads(json.dumps(result))
    diverging["metrics"]["first_post_fork_divergence_rate"] = 0.0667
    assert "the fixture model must not diverge" in check(diverging)[0]
    missing = json.loads(json.dumps(result))
    missing["metrics"]["sibling_failure_rate"] = None
    assert check(missing) == ["sibling_failure_rate is null"]
    excluded = json.loads(json.dumps(result))
    excluded["metadata"]["verdict_agreement_pairs"][0]["excluded"] = "4 completed seed(s)"
    assert len(check(excluded)) == 1


def test_the_stand_in_replaces_every_live_model_and_nothing_else():
    stand_in = _dry_run_module().stand_in(PLAN.model_dump(mode="json"))
    assert stand_in["experiment_id"] == "exp_001_replay_validity_dry_run"
    for before, after in zip(PLAN.conditions, stand_in["conditions"], strict=True):
        assert after["agent_config"]["provider"] == "fixture"
        assert "cassette" not in after["agent_config"] or after["agent_config"]["cassette"] is None
        assert (after["seeds"], after["control_ids"], after["start"]) == (
            before.seeds,
            before.control_ids,
            before.start.model_dump(),
        )


def test_the_stand_in_is_written_as_record_writes_the_plan():
    """retain_exp_001.sh compares the two, so a frozen plan's stand-in must match (r13-1).

    A frozen plan is not frozen again in the rehearsal, so nothing rewrites the
    stand-in before record reads it and writes it back through the model.
    """
    stand_in = _dry_run_module().stand_in(json.loads((EXP_DIR / "experiment.json").read_text()))
    assert ExperimentSpec.model_validate(stand_in).model_dump(mode="json") == stand_in


def test_regenerate_imports_the_cli_before_refusing_sockets(dry_run, tmp_path):
    """An HTTP client imported with the CLI imports ssl, which needs the real socket class."""
    copy = tmp_path / "retained"
    shutil.copytree(dry_run.retained, copy)
    wrapper = tmp_path / "python_importing_ssl_with_the_cli.py"
    wrapper.write_text(
        "import importlib.abc, sys\n"
        "class ImportSslWithTheCli(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'trace_harness.cli':\n"
        "            import ssl  # noqa: F401\n"
        "        return None\n"
        "sys.meta_path.insert(0, ImportSslWithTheCli())\n"
        "sys.argv = sys.argv[1:]\n"
        "exec(compile(sys.stdin.read(), '<stdin>', 'exec'), {'__name__': '__main__'})\n"
    )
    python = tmp_path / "python"
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{wrapper}" "$@"\n')
    python.chmod(0o755)
    ran = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "regenerate_exp_001.sh"), str(copy)],
        capture_output=True,
        text=True,
        env={**_env(), "PYTHON": str(python)},
    )
    assert ran.returncode == 0, ran.stdout[-2000:] + ran.stderr[-2000:]
    assert "result.json: identical" in ran.stdout


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
    retained = dry_run.retained
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


def _estimate_module():
    spec = importlib.util.spec_from_file_location(
        "estimate_exp_001_cost", REPO_ROOT / "scripts" / "estimate_exp_001_cost.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_cost_estimate_prices_through_the_adapter_tables():
    """Prices are read from the tables, so a price change in code moves both sides."""
    result = _estimate_module().estimate()
    assert result["cassette_inputs"] == [995, 1251, 1750, 2851, 3573]
    assert result["cassette_max_output"] == result["output_tokens_per_call"] == 690
    rows = {r["condition"]: r for r in result["rows"]}
    assert len(rows) == 9
    # refund_policy_failure forks at 5 and the recording has 2 steps after it.
    # Step 6 sends 3573 + 1101 = 4674 input tokens and step 7 sends 5775, each
    # answered with 690 output tokens. The harness rounds a run's cost to the
    # micro-dollar.
    calls = [(4674, 690), (5775, 690)]
    prices = {
        "live": GEMINI_PRICING["gemini-3.6-flash"],
        "live_no_control": GEMINI_PRICING["gemini-3.6-flash"],
        "live_swapped": ANTHROPIC_PRICING["claude-sonnet-5"],
    }
    for arm, (per_input, per_output) in prices.items():
        per_run = round(sum(i * per_input + o * per_output for i, o in calls) / 1_000_000, 6)
        assert rows[f"{arm}__refund_policy_failure"]["expected_usd"] == pytest.approx(5 * per_run)
    assert result["expected_usd"] == round(sum(r["expected_usd"] for r in rows.values()), 2)
    assert result["high_usd"] == round(sum(r["high_usd"] for r in rows.values()), 2)
    assert result["high_usd"] <= result["cap_usd"] == 50.0


def _runbook_cost_table() -> dict[str, tuple[float, float]]:
    text = (REPO_ROOT / "docs" / "experiments" / "runbook_001.md").read_text()
    section = text.split("## Cost against the cap")[1].split("\n## ")[0]
    rows = re.findall(r"^\| `?(\w+)`? \|[^|]*\| \$([\d.]+) \| \$([\d.]+) \|$", section, re.M)
    return {arm: (float(expected), float(high)) for arm, expected, high in rows}


def test_the_runbook_cost_table_is_the_estimate_at_the_prices_it_states():
    """The runbook's prices are the adapter tables', claude-sonnet-5 at 2 and 10."""
    assert GEMINI_PRICING["gemini-3.6-flash"] == (0.75, 3.75)
    assert ANTHROPIC_PRICING["claude-sonnet-5"] == (2.0, 10.0)
    module = _estimate_module()
    result = module.estimate()
    by_arm: dict[str, list[float]] = {}
    for row in result["rows"]:
        arm = row["condition"].split("__")[0]
        totals = by_arm.setdefault(arm, [0.0, 0.0])
        totals[0] += row["expected_usd"]
        totals[1] += row["high_usd"]
    expected = {arm: (round(e, 2), round(h, 2)) for arm, (e, h) in by_arm.items()}
    expected["Total"] = (result["expected_usd"], result["high_usd"])
    assert (
        _runbook_cost_table()
        == expected
        == {
            "live": (0.14, 3.53),
            "live_no_control": (0.14, 3.53),
            "live_swapped": (0.38, 9.4),
            "Total": (0.66, 16.45),
        }
    )
    runbook = (REPO_ROOT / "docs" / "experiments" / "runbook_001.md").read_text()
    wide = module.estimate(output_tokens=1010)
    assert (wide["expected_usd"], wide["high_usd"]) == (0.8, 18.47)
    assert "gives $0.80 expected and $18.47 high" in runbook
    assert "at 2 and 10 for claude-sonnet-5" in runbook
