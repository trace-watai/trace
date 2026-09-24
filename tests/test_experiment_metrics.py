"""The metrics ``experiment record`` derives from batch summaries (#155).

Each test pins one clause of the Part B2 formulas in
``docs/methodology_metrics.md``. The counts are deliberately lopsided, since a
batch with one pass and one fail cannot tell counting failures from counting
passes.
"""

from __future__ import annotations

import json

from conftest import REPO_ROOT
from test_experiment import _entry, _spec, _summary
from trace_harness.cli import main
from trace_harness.run_reader import RunReader
from trace_harness.runner.batch import BatchSummary
from trace_harness.runner.experiment import ExperimentMetrics, ExperimentResult, derive_metrics
from trace_harness.tracing.artifact_store import ArtifactStore

RETAINED = REPO_ROOT / "docs" / "acceptance"


def _terminated(task_id: str, *, latency: float) -> object:
    return _entry(task_id, "incomplete", latency=latency).model_copy(
        update={"status": "terminated", "verifier_passed": False}
    )


# --- verified_failure_count ---


def test_verified_failures_count_fails_and_nothing_else() -> None:
    entries = [
        _entry("f1", "fail"),
        _entry("f2", "fail"),
        _entry("f3", "fail"),
        _entry("p", "pass"),
    ]
    assert derive_metrics([_summary("b", entries)]).verified_failure_count == 3


def test_an_incomplete_run_is_not_a_verified_failure() -> None:
    entries = [_entry("f", "fail"), _terminated("t1", latency=1.0), _terminated("t2", latency=1.0)]
    assert derive_metrics([_summary("b", entries)]).verified_failure_count == 1


def test_a_fail_verdict_on_a_run_that_did_not_complete_is_not_counted() -> None:
    """Summaries written before verifier 0.4.0 can carry one; B2 counts completed runs."""
    stale = _entry("s", "fail").model_copy(update={"status": "terminated"})
    assert derive_metrics([_summary("b", [_entry("f", "fail"), stale])]).verified_failure_count == 1


def test_nothing_recorded_measures_nothing() -> None:
    """No batch, or no verified run, is an absent count and never a zero."""
    empty = derive_metrics([])
    assert empty == ExperimentMetrics()
    only_incomplete = derive_metrics([_summary("b", [_terminated("t", latency=1.0)])])
    assert only_incomplete.verified_failure_count is None
    assert only_incomplete.latency_ms_p50 is None


def test_recording_no_condition_leaves_every_metric_unmeasured(tmp_path) -> None:
    runs = tmp_path / "runs"
    plan = tmp_path / "experiment.json"
    plan.write_text(_spec().model_dump_json(indent=2), encoding="utf-8")
    assert main(["--runs-dir", str(runs), "experiment", "record", str(plan)]) == 0

    _, result = RunReader(ArtifactStore(runs)).get_experiment(_spec().experiment_id)
    assert result is not None
    assert result.metrics == ExperimentMetrics()


# --- cost and latency ---


def test_cost_reports_how_many_runs_recorded_one() -> None:
    entries = [
        _entry("a", "pass", cost=0.5),
        _entry("b", "pass"),
        _entry("c", "fail", cost=1.0),
    ]
    metrics = derive_metrics([_summary("b1", entries)])
    assert metrics.cost_usd == 1.5
    assert metrics.extra["cost_recorded_k"] == 2
    assert metrics.extra["cost_recorded_n"] == 3


def test_latency_is_the_median_of_completed_runs_only() -> None:
    entries = [
        _entry("a", "pass", latency=100.0),
        _entry("b", "fail", latency=300.0),
        _terminated("t", latency=1.0),
    ]
    assert derive_metrics([_summary("b1", entries)]).latency_ms_p50 == 200.0


# --- more than one condition ---


def test_each_condition_is_also_reported_on_its_own() -> None:
    replay = _summary(
        "b_replay", [_entry("r1", "fail", latency=2.0), _entry("r2", "pass", latency=4.0)]
    )
    live = _summary(
        "b_live",
        [
            _entry("l1", "fail", latency=900.0),
            _entry("l2", "fail", latency=1100.0),
            _entry("l3", "fail", latency=1000.0),
        ],
    )
    metrics = derive_metrics(
        [replay, live], condition_names={"b_replay": "replay_only", "b_live": "live_on"}
    )

    assert metrics.verified_failure_count == 4
    assert metrics.latency_ms_p50 == 900.0
    assert metrics.extra["verified_failure_count.replay_only"] == 1
    assert metrics.extra["verified_failure_count.live_on"] == 3
    assert metrics.extra["latency_ms_p50.replay_only"] == 3.0
    assert metrics.extra["latency_ms_p50.live_on"] == 1000.0


def test_one_condition_adds_no_per_condition_keys() -> None:
    summary = _summary("b", [_entry("a", "fail", latency=1.0)])
    metrics = derive_metrics([summary], condition_names={"b": "replay_only"})
    assert not [key for key in metrics.extra if "." in key]


def test_one_batch_cannot_answer_two_conditions(tmp_path, capsys) -> None:
    """Pooling would count its runs twice."""
    runs = tmp_path / "runs"
    store = ArtifactStore(runs)
    store.write_batch_summary("b", _summary("b", [_entry("a", "fail")]))
    live = _spec().conditions[0].model_copy(update={"name": "live_on"})
    spec = _spec(conditions=[_spec().conditions[0], live])
    plan = tmp_path / "experiment.json"
    plan.write_text(spec.model_dump_json(indent=2), encoding="utf-8")
    capsys.readouterr()

    argv = ["--condition", "replay_only=b", "--condition", "live_on=b"]
    assert main(["--runs-dir", str(runs), "experiment", "record", str(plan), *argv]) == 2
    assert "gives batch b to two conditions" in capsys.readouterr().err


# --- the acceptance number ---


def test_the_retained_metrics_rederive_from_the_retained_batch() -> None:
    """exp_000's five failures re-derive from its retained batch summary."""
    result = ExperimentResult.model_validate_json(
        (RETAINED / "experiments" / "exp_000_baseline" / "result.json").read_text()
    )
    (batch_id,) = result.condition_batches.values()
    summary = BatchSummary.model_validate_json(
        (RETAINED / "batches" / batch_id / "batch_summary.json").read_text()
    )
    derived = derive_metrics([summary])

    assert derived.verified_failure_count == 5
    for name in ExperimentMetrics.memo_field_names():
        assert getattr(derived, name) == getattr(result.metrics, name), name


def test_a_fresh_baseline_run_records_five_verified_failures(tmp_path, monkeypatch) -> None:
    """The acceptance criterion, end to end from a fresh refund_bundles_v0 batch."""
    monkeypatch.chdir(REPO_ROOT)
    runs = tmp_path / "runs"
    suite = "fixtures/suites/refund_bundles_v0.json"
    assert main(["--runs-dir", str(runs), "run-suite", suite]) == 0
    (batch_dir,) = (runs / "batches").iterdir()

    plan = json.loads(
        (RETAINED / "experiments" / "exp_000_baseline" / "experiment.json").read_text()
    )
    plan["experiment_id"] = "exp_fresh_baseline"
    plan_path = tmp_path / "experiment.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    argv = ["experiment", "record", str(plan_path), "--condition", f"replay_only={batch_dir.name}"]
    assert main(["--runs-dir", str(runs), *argv]) == 0

    _, result = RunReader(ArtifactStore(runs)).get_experiment("exp_fresh_baseline")
    assert result is not None
    assert result.metrics.verified_failure_count == 5
    assert result.metrics.extra["cost_recorded_n"] == 9
