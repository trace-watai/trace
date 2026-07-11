"""RunReader: typed reads over the runs/ store, with explicit missing states.

Drives real repo fixtures (via conftest) into temp runs dirs — no synthetic
artifacts, offline always. ``failure_run`` is a fixture-only run (no verifier/
attribution/bundle yet), so it exercises the not-yet-produced -> None states;
a full ``run-pipeline`` via the CLI exercises the populated states.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import FAILURE_TASK_PATH, FixtureRun
from trace_harness.attribution.schemas import AttributionResult
from trace_harness.cli import main
from trace_harness.failure_bundles.generator import FailureBundle
from trace_harness.run_reader import RunNotFound, RunReader, RunSummary
from trace_harness.runner.result import RunResult
from trace_harness.tasks.schemas import TaskSpec
from trace_harness.tracing import artifact_store as names
from trace_harness.tracing.events import TraceEvent
from trace_harness.verifiers.base import VerifierResult


def test_list_runs_returns_summaries_matching_run_result(failure_run: FixtureRun) -> None:
    reader = RunReader(failure_run.store)
    summaries = reader.list_runs()

    assert len(summaries) == 1
    summary = summaries[0]
    assert isinstance(summary, RunSummary)
    result = failure_run.result
    assert summary.run_id == result.run_id
    assert summary.task_id == result.task_id
    assert summary.status == str(result.status)
    assert summary.termination_reason == str(result.termination_reason)
    assert summary.steps_taken == result.steps_taken
    # verifier hasn't run yet — verdict fields are absent
    assert summary.verifier_passed is None
    assert summary.failed_check_count is None


def test_list_runs_chronological_and_skips_resultless_dirs(failure_run: FixtureRun) -> None:
    store = failure_run.store
    # A directory with no run_result.json (e.g. crashed before writing) is skipped,
    # not fatal.
    store.create_run_dir("run_00000000T000000Z_empty")
    summaries = RunReader(store).list_runs()
    assert [s.run_id for s in summaries] == [failure_run.run_id]


def test_list_runs_rebuilds_nonempty_incomplete_index(failure_run: FixtureRun) -> None:
    store = failure_run.store
    missing_run_id = "run_99999999T999999Z_missing"
    store.create_run_dir(missing_run_id)
    store.write_json(
        missing_run_id,
        names.RUN_RESULT,
        failure_run.result.model_copy(update={"run_id": missing_run_id}),
    )

    summaries = RunReader(store).list_runs()

    assert [summary.run_id for summary in summaries] == [failure_run.run_id, missing_run_id]


def test_list_runs_empty_when_no_runs(tmp_path: Path) -> None:
    assert RunReader.from_runs_dir(tmp_path / "runs").list_runs() == []


def test_get_run_and_task_and_trace(failure_run: FixtureRun) -> None:
    reader = RunReader(failure_run.store)
    run_id = failure_run.run_id

    assert isinstance(reader.get_run(run_id), RunResult)
    assert reader.get_run(run_id).run_id == run_id
    assert isinstance(reader.get_task(run_id), TaskSpec)
    trace = reader.get_trace(run_id)
    assert trace and all(isinstance(e, TraceEvent) for e in trace)


def test_unknown_run_raises_run_not_found(failure_run: FixtureRun) -> None:
    reader = RunReader(failure_run.store)
    with pytest.raises(RunNotFound, match="run not found: 'nope'"):
        reader.get_run("nope")
    with pytest.raises(RunNotFound):
        reader.get_trace("nope")
    with pytest.raises(RunNotFound):
        reader.get_verifier("nope")


def test_downstream_artifacts_are_none_before_their_stage(failure_run: FixtureRun) -> None:
    reader = RunReader(failure_run.store)
    run_id = failure_run.run_id
    # failure_run only ran the fixture stage; verify/attribute/bundle haven't run.
    assert reader.get_verifier(run_id) is None
    assert reader.get_attribution(run_id) is None
    assert reader.get_bundle(run_id) is None


def test_downstream_artifacts_populated_after_pipeline(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    exit_code = main(["--runs-dir", str(runs_dir), "run-pipeline", str(FAILURE_TASK_PATH)])
    assert exit_code == 0  # a verified failure without --fail-on-verifier is success

    # Simulate pre-index history: listing rebuilds from source artifacts and
    # preserves the verifier verdict instead of returning an incomplete summary.
    (runs_dir / "index.json").unlink()
    reader = RunReader.from_runs_dir(runs_dir)
    (summary,) = reader.list_runs()
    run_id = summary.run_id

    verifier = reader.get_verifier(run_id)
    assert isinstance(verifier, VerifierResult)
    assert not verifier.passed  # the failure fixture fails verification

    assert isinstance(reader.get_attribution(run_id), AttributionResult)

    bundle = reader.get_bundle(run_id)
    assert isinstance(bundle, FailureBundle)
    assert bundle.failure_card and bundle.repair_package and bundle.regression_artifact

    # After verify runs, list_runs should surface the verdict in the summary.
    assert summary.verifier_passed is False
    assert summary.failed_check_count is not None and summary.failed_check_count > 0


def test_cli_list_runs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runs_dir = tmp_path / "runs"
    assert main(["--runs-dir", str(runs_dir), "run-pipeline", str(FAILURE_TASK_PATH)]) == 0
    capsys.readouterr()  # drop pipeline output

    assert main(["--runs-dir", str(runs_dir), "list-runs"]) == 0
    out = capsys.readouterr().out
    run_id = RunReader.from_runs_dir(runs_dir).list_runs()[0].run_id
    assert run_id in out
    assert "1 run(s)" in out


def test_cli_list_runs_empty(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runs_dir = tmp_path / "runs"
    assert main(["--runs-dir", str(runs_dir), "list-runs"]) == 0
    assert "no runs found" in capsys.readouterr().out
