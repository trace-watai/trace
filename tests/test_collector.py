"""Exercise the release gate against real retained and freshly generated artifacts."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from conftest import FIXTURES_DIR, REPO_ROOT
from trace_harness.cli import main
from trace_harness.runner.collector import SUMMARY_NAME, CollectorSummary, collect_regressions
from trace_harness.runner.pipeline import run_task_pipeline
from trace_harness.runner.suite import AgentConfig
from trace_harness.tracing.artifact_store import ArtifactStore

RETAINED_ROOT = REPO_ROOT / "docs/acceptance/runs"
RETAINED_EXPERIMENTS = REPO_ROOT / "docs/acceptance/experiments"
RETAINED = RETAINED_ROOT / "run_20260820T012748Z_0e9c6172/regression_artifact.json"
BUNDLE_SUITE = FIXTURES_DIR / "suites/refund_bundles_v0.json"
CONTROL_DEMO = FIXTURES_DIR / "tasks/refund_policy_control_demo.json"


@pytest.fixture(autouse=True)
def offline_repo(monkeypatch):
    monkeypatch.chdir(REPO_ROOT)

    def forbidden(*args, **kwargs):
        pytest.fail("regression collection attempted to use the network")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


def copy_artifact(directory: Path, source: Path = RETAINED, **updates) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    data = json.loads(source.read_text())
    data.update(updates)
    path = directory / "regression_artifact.json"
    path.write_text(json.dumps(data))
    return path


def demo_artifact(tmp_path: Path, **updates) -> Path:
    store = ArtifactStore(tmp_path / "demo-source")
    result = run_task_pipeline(CONTROL_DEMO, AgentConfig(label="fixture"), store)
    return copy_artifact(
        tmp_path / "demo-input",
        store.artifact_path(result.run_result.run_id, "regression_artifact.json"),
        **updates,
    )


def collect(
    source: Path, tmp_path: Path, suite: Path | None = None, experiments: Path | None = None
) -> CollectorSummary:
    output = tmp_path / "collected"
    summary = collect_regressions(
        source, ArtifactStore(output), suite_path=suite, experiments_path=experiments
    )
    saved = (output / SUMMARY_NAME).read_text()
    assert CollectorSummary.model_validate_json(saved) == summary
    assert (Path(summary.collection_dir) / SUMMARY_NAME).read_text() == saved
    return summary


def test_the_gate_in_check_repo_runs_this_collection():
    """The test below is the collection scripts/check_repo.sh runs, with the same inputs."""
    script = " ".join(
        (REPO_ROOT / "scripts/check_repo.sh").read_text().replace("\\\n", " ").split()
    )
    assert (
        "collect-regressions docs/acceptance/runs --suite fixtures/suites/refund_bundles_v0.json "
        "--experiments docs/acceptance/experiments"
    ) in script
    assert RETAINED_ROOT.relative_to(REPO_ROOT).as_posix() == "docs/acceptance/runs"
    assert (
        BUNDLE_SUITE.relative_to(REPO_ROOT).as_posix() == "fixtures/suites/refund_bundles_v0.json"
    )
    assert RETAINED_EXPERIMENTS.relative_to(REPO_ROOT).as_posix() == "docs/acceptance/experiments"


def test_retained_plus_bundle_suite_reproduces_eight_with_advisory_controls(tmp_path):
    before = RETAINED.read_bytes()
    summary = collect(RETAINED_ROOT, tmp_path, BUNDLE_SUITE, RETAINED_EXPERIMENTS)
    assert summary.exit_code == 0
    # Three retained artifacts (the refund_v0 failure and the two reference
    # outside-agent runs from #210) plus five generated from the bundle suite.
    assert summary.artifacts_found == summary.blocking == summary.reproduced == 8
    # The retained baseline (#195) and the exp_001 plan (#200), which has no
    # result until its live run; neither has drifted.
    assert [e.experiment_id for e in summary.experiments] == [
        "exp_000_baseline",
        "exp_001_replay_validity",
    ]
    assert summary.experiments_drifted == []
    assert summary.siblings_passed == 8
    assert summary.not_reproduced == summary.siblings_failed == summary.malformed == []
    assert summary.controls_confirmed == 0
    assert summary.controls_advisory == 8
    assert summary.entries[0].baseline.scenario.completed
    assert all(entry.control_status == "advisory" for entry in summary.entries)
    assert any(entry.control.exit_code == 1 for entry in summary.entries)
    # Retained + newly generated versions share a name but are separate evidence.
    assert sum(e.test_name == "regression_refund_policy_failure" for e in summary.entries) == 4
    assert Path(summary.suite_summary_path).is_file()
    for entry in summary.entries:
        evidence = Path(entry.evidence_dir)
        assert (evidence / "baseline/replay.log").is_file()
        assert (evidence / "control/replay.log").is_file()
        assert (evidence / "baseline" / entry.baseline.scenario.run_id / "trace.jsonl").is_file()
    assert RETAINED.read_bytes() == before


def test_a_check_that_does_not_fire_fails_reproduction(tmp_path):
    path = copy_artifact(tmp_path / "input", verifier_checks=["check_that_will_not_fire"])
    summary = collect(path, tmp_path)
    assert summary.exit_code == 1
    assert summary.reproduced == 0
    assert summary.not_reproduced == ["regression_refund_policy_failure"]


def test_failing_positive_sibling_is_named_in_summary_and_cli(tmp_path, capsys):
    sibling = "valid_refund_must_still_work"
    path = copy_artifact(
        tmp_path / "input",
        positive_sibling_tests=[
            {"test_name": sibling, "task_fixture": "fixtures/tasks/refund_policy_failure.json"}
        ],
    )
    output = tmp_path / "cli"
    assert main(["collect-regressions", str(path), "--runs-dir", str(output)]) == 1
    summary = CollectorSummary.model_validate_json((output / SUMMARY_NAME).read_text())
    assert summary.reproduced == 1
    assert summary.siblings_failed == [("regression_refund_policy_failure", sibling)]
    assert sibling in capsys.readouterr().out


@pytest.mark.parametrize("mode", ["unlabeled", "live_required", "static_ok"])
def test_control_failure_only_gates_when_static_ok(tmp_path, mode):
    path = copy_artifact(tmp_path / "input", replay_mode=mode)
    summary = collect(path, tmp_path)
    assert summary.entries[0].control.exit_code == 1
    assert summary.exit_code == (1 if mode == "static_ok" else 0)
    assert summary.controls_advisory == (0 if mode == "static_ok" else 1)
    assert summary.controls_failed == (
        ["regression_refund_policy_failure"] if mode == "static_ok" else []
    )


@pytest.mark.parametrize("mode", [None, "live_required", "static_ok"])
def test_clean_control_replay_is_only_confirmed_for_explicit_static_ok(tmp_path, mode):
    # This label is a synthetic consumer input; #156 owns real classification.
    path = demo_artifact(tmp_path, **({"replay_mode": mode} if mode else {}))
    summary = collect(path, tmp_path)
    assert summary.exit_code == 0
    assert summary.entries[0].control.exit_code == 0
    assert summary.controls_confirmed == (1 if mode == "static_ok" else 0)
    assert summary.controls_advisory == (0 if mode == "static_ok" else 1)


def test_incomplete_run_cannot_count_as_reproduced_even_if_pinned_check_fires(tmp_path):
    path = demo_artifact(tmp_path, replay_mode="static_ok")
    data = json.loads(path.read_text())
    data["pinned_agent_actions"] = data["pinned_agent_actions"][:-1]  # no final answer
    path.write_text(json.dumps(data))
    summary = collect(path, tmp_path)
    entry = summary.entries[0]
    # An incomplete replay fails the gate on its own (#163): a run that never
    # reached a final answer cannot establish a regression verdict, whatever
    # its checks happened to report before it died.
    assert entry.baseline.exit_code == 1
    assert "unauthorized_cash_refund" in entry.baseline.scenario.failed_checks
    assert entry.baseline.scenario.completed is False
    assert summary.reproduced == 0
    assert summary.exit_code == 1
    assert entry.control.scenario.completed is False
    assert summary.controls_confirmed == 0
    assert summary.controls_failed == [entry.test_name]


def test_control_cannot_be_confirmed_if_baseline_failure_disappeared(tmp_path):
    path = demo_artifact(tmp_path, replay_mode="static_ok")
    data = json.loads(path.read_text())
    data["initial_state"]["orders"][0]["purchase_age_days"] = 5
    path.write_text(json.dumps(data))
    summary = collect(path, tmp_path)
    assert summary.entries[0].control.exit_code == 0
    assert summary.reproduced == summary.controls_confirmed == 0
    assert summary.exit_code == 1
    assert summary.entries[0].control_error == "baseline gate failed; control cannot be confirmed"


def test_nonblocking_artifact_is_listed_without_replay(tmp_path):
    path = copy_artifact(
        tmp_path / "input",
        blocks_release=False,
        task_fixture="missing-task.json",
        verifier_checks=[],
    )
    summary = collect(path, tmp_path)
    assert summary.exit_code == 0
    assert summary.artifacts_found == 1
    assert summary.blocking == summary.reproduced == summary.controls_advisory == 0
    assert summary.skipped == ["regression_refund_policy_failure"]
    assert summary.entries[0].baseline is None


@pytest.mark.parametrize(
    "damage",
    [
        {"verifier_checks": []},
        {"replay_mode": "static_ko"},
        {"replay_mode": None},
        {"blocks_release": "false"},
        {"pinned_agent_actions": [{"kind": "unknown"}]},
        {"task_fixture": "missing-task.json"},
        {"positive_sibling_tests": [{"test_name": "missing", "task_fixture": "missing.json"}]},
    ],
)
def test_malformed_artifact_or_unusable_inputs_exit_two(tmp_path, damage):
    path = copy_artifact(tmp_path / "input", **damage)
    summary = collect(path, tmp_path)
    assert summary.exit_code == 2
    assert summary.malformed == [str(path)]
    assert summary.entries[0].error


def test_malformed_json_does_not_hide_other_results_and_exit_two_wins(tmp_path):
    copy_artifact(tmp_path / "input/good")
    bad = copy_artifact(tmp_path / "input/bad")
    bad.write_text('{"test_name":')
    copy_artifact(tmp_path / "input/fails", verifier_checks=["not_observed"])
    summary = collect(tmp_path / "input", tmp_path)
    assert summary.artifacts_found == 3
    assert summary.reproduced == 1
    assert summary.not_reproduced == ["regression_refund_policy_failure"]
    assert summary.exit_code == 2
    assert summary.malformed == [str(bad)]


def test_missing_path_is_an_input_error_and_empty_directory_is_reported(tmp_path):
    summary = collect(tmp_path / "missing", tmp_path)
    assert summary.exit_code == 2
    assert summary.artifacts_found == 0
    empty = tmp_path / "empty"
    empty.mkdir()
    summary = collect(empty, tmp_path)
    assert summary.exit_code == 0
    assert summary.artifacts_found == summary.blocking == 0


def test_output_creation_does_not_turn_missing_input_into_success(tmp_path):
    missing = tmp_path / "new-runs"
    summary = collect_regressions(missing, ArtifactStore(missing))
    assert summary.exit_code == 2
    assert summary.malformed == [str(missing)]
    assert (missing / SUMMARY_NAME).is_file()


def test_broken_artifact_symlink_is_reported_as_malformed(tmp_path):
    source = tmp_path / "input"
    source.mkdir()
    broken = source / "regression_artifact.json"
    broken.symlink_to(source / "missing.json")
    summary = collect(source, tmp_path)
    assert summary.artifacts_found == 1
    assert summary.malformed == [str(broken)]
    assert summary.exit_code == 2


def test_repeated_collection_does_not_discover_its_own_copies_or_execute_commands(tmp_path):
    marker = tmp_path / "must-not-exist"
    source = copy_artifact(tmp_path / "input", replay_command=f"touch {marker}")
    before = source.read_bytes()
    output = ArtifactStore(tmp_path / "input/output")
    for _ in range(2):
        summary = collect_regressions(tmp_path / "input", output)
        assert summary.exit_code == 0
        assert summary.artifacts_found == summary.reproduced == 1
    assert source.read_bytes() == before
    assert not marker.exists()


def test_suite_failure_is_not_hidden_by_zero_generated_artifacts(tmp_path):
    suite = tmp_path / "suite.json"
    suite.write_text(json.dumps({"suite_id": "broken", "tasks": ["missing-task.json"]}))
    summary = collect(RETAINED, tmp_path, suite)
    assert summary.exit_code == 1
    assert summary.reproduced == 1
    assert "missing-task.json" in summary.errors[0]


def test_live_suite_is_rejected_without_constructing_a_provider(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("collector attempted to construct a live provider")

    monkeypatch.setattr("trace_harness.models.gemini.GeminiModelAdapter.__init__", forbidden)
    suite = tmp_path / "suite.json"
    suite.write_text(
        json.dumps(
            {
                "suite_id": "live",
                "tasks": ["fixtures/tasks/refund_policy_failure.json"],
                "agent_configs": [{"label": "live", "provider": "gemini"}],
            }
        )
    )
    summary = collect(RETAINED, tmp_path, suite)
    assert summary.exit_code == 2
    assert summary.malformed == [str(suite)]
    assert summary.reproduced == 1  # independent retained evidence still ran


@pytest.mark.parametrize("mode", ["unlabeled", "static_ok"])
def test_control_runtime_error_retains_advisory_or_gating_semantics(tmp_path, monkeypatch, mode):
    from trace_harness.runner import collector

    original = collector._replay

    def broken_control(artifact, evidence_dir, *, apply_control):
        if apply_control:
            raise RuntimeError("control validation unavailable")
        return original(artifact, evidence_dir, apply_control=apply_control)

    monkeypatch.setattr(collector, "_replay", broken_control)
    path = copy_artifact(tmp_path / "input", replay_mode=mode)
    summary = collect(path, tmp_path)
    assert summary.exit_code == (1 if mode == "static_ok" else 0)
    assert summary.entries[0].control_error == "control validation unavailable"
    assert summary.controls_confirmed == 0
