"""What ``experiment record`` may write, and what it must refuse (#155).

The plan exists so the hypothesis and the frozen manifest cannot be adjusted
after the numbers come in. Recording is the moment that could quietly undo
that, so every refusal here is checked to leave the stored files untouched.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from conftest import REPO_ROOT
from test_experiment import _entry, _spec, _summary
from trace_harness.cli import main
from trace_harness.run_reader import RunReader
from trace_harness.runner.experiment import (
    ExperimentResult,
    ExperimentSpec,
    load_plan,
    render_experiment_markdown,
)
from trace_harness.tracing.artifact_store import ArtifactStore

BATCH = "batch_20260101T000000Z_aaaaaaaa"
ACCEPTANCE = REPO_ROOT / "docs" / "acceptance"
RETAINED = ACCEPTANCE / "experiments" / "exp_000_baseline"


def _store_with_batch(tmp_path: Path, *, suite_id: str = "refund_bundles_v0") -> ArtifactStore:
    store = ArtifactStore(tmp_path / "runs")
    summary = _summary(BATCH, [_entry("t1", "fail"), _entry("t2", "pass")])
    store.write_batch_summary(BATCH, summary.model_copy(update={"suite_id": suite_id}))
    return store


def _write_plan(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def _record(store: ArtifactStore, plan: Path, *extra: str) -> int:
    return main(["--runs-dir", str(store.runs_dir), "experiment", "record", str(plan), *extra])


def _plan_data() -> dict:
    return json.loads(_spec().model_dump_json())


# --- the plan is stored once and never rewritten ---


@pytest.mark.parametrize("field", ["experiment_id", "created_at"])
def test_a_plan_file_must_state_its_id_and_creation_time(tmp_path, capsys, field) -> None:
    """A defaulted id would mint a new experiment on every record of one file."""
    store = _store_with_batch(tmp_path)
    data = _plan_data()
    del data[field]
    plan = _write_plan(tmp_path / "plan.json", data)
    capsys.readouterr()

    assert _record(store, plan, "--condition", f"replay_only={BATCH}") == 2
    assert f"must state {field}" in capsys.readouterr().err
    assert not (store.runs_dir / "experiments").exists()
    with pytest.raises(ValueError, match=f"must state {field}"):
        load_plan(data)


def test_the_first_record_stores_the_plan(tmp_path) -> None:
    store = _store_with_batch(tmp_path)
    data = _plan_data()
    plan = _write_plan(tmp_path / "plan.json", data)

    assert _record(store, plan, "--condition", f"replay_only={BATCH}") == 0
    stored = load_plan(store.read_experiment_spec(data["experiment_id"]))
    assert stored.model_dump(mode="json") == load_plan(data).model_dump(mode="json")


def test_recording_again_never_rewrites_the_stored_plan(tmp_path) -> None:
    """Stored in a layout record would never write, so any rewrite shows."""
    store = _store_with_batch(tmp_path)
    data = _plan_data()
    stored_path = store.experiment_spec_path(data["experiment_id"])
    stored_path.parent.mkdir(parents=True)
    stored_path.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    before = stored_path.read_bytes()
    plan = _write_plan(tmp_path / "plan.json", data)

    assert _record(store, plan, "--condition", f"replay_only={BATCH}") == 0
    assert _record(store, plan, "--decision", "review", "--condition", f"replay_only={BATCH}") == 0

    assert stored_path.read_bytes() == before
    assert [p.name for p in (store.runs_dir / "experiments").iterdir()] == [data["experiment_id"]]
    _, result = RunReader(store).get_experiment(data["experiment_id"])
    assert result is not None and result.decision.value == "review"


def test_a_plan_that_differs_from_the_stored_one_is_refused(tmp_path, capsys) -> None:
    store = _store_with_batch(tmp_path)
    data = _plan_data()
    first = _write_plan(tmp_path / "plan.json", data)
    assert _record(store, first, "--condition", f"replay_only={BATCH}") == 0
    stored_path = store.experiment_spec_path(data["experiment_id"])
    result_path = store.experiment_result_path(data["experiment_id"])
    stored_before, result_before = stored_path.read_bytes(), result_path.read_bytes()

    changed = _write_plan(
        tmp_path / "changed.json", {**data, "hypothesis": "whatever the numbers turned out to say"}
    )
    capsys.readouterr()
    assert _record(store, changed, "--condition", f"replay_only={BATCH}") == 2
    assert "already holds a different plan" in capsys.readouterr().err
    assert stored_path.read_bytes() == stored_before
    assert result_path.read_bytes() == result_before


def test_a_condition_named_twice_is_refused(tmp_path, capsys) -> None:
    store = _store_with_batch(tmp_path)
    plan = _write_plan(tmp_path / "plan.json", _plan_data())
    capsys.readouterr()
    pairs = ["--condition", f"replay_only={BATCH}", "--condition", "replay_only=batch_other"]
    assert _record(store, plan, *pairs) == 2
    assert "--condition names 'replay_only' twice" in capsys.readouterr().err
    assert not (store.runs_dir / "experiments").exists()


def test_a_plan_whose_id_leaves_the_runs_directory_is_refused(tmp_path, capsys) -> None:
    store = _store_with_batch(tmp_path)
    plan = _write_plan(tmp_path / "plan.json", {**_plan_data(), "experiment_id": "../../escaped"})
    capsys.readouterr()
    assert _record(store, plan, "--condition", f"replay_only={BATCH}") == 2
    assert not (tmp_path / "escaped").exists()
    assert not (store.runs_dir / "experiments").exists()


# --- the frozen manifest ---


def test_a_batch_from_another_suite_is_refused(tmp_path, capsys) -> None:
    store = _store_with_batch(tmp_path, suite_id="some_other_suite")
    plan = _write_plan(tmp_path / "plan.json", _plan_data())
    capsys.readouterr()

    assert _record(store, plan, "--condition", f"replay_only={BATCH}") == 2
    err = capsys.readouterr().err
    assert "freezes suite 'refund_bundles_v0'" in err
    assert "replay_only ran 'some_other_suite'" in err
    assert not (store.runs_dir / "experiments").exists()


# --- the retained baseline ---


def test_the_retained_baseline_still_records(tmp_path) -> None:
    """exp_000 records against a copy of itself without its plan being touched."""
    runs = tmp_path / "acceptance"
    shutil.copytree(ACCEPTANCE / "experiments", runs / "experiments")
    shutil.copytree(ACCEPTANCE / "batches", runs / "batches")
    plan = runs / "experiments" / "exp_000_baseline" / "experiment.json"
    before = plan.read_bytes()
    (batch_id,) = json.loads((RETAINED / "result.json").read_text())["condition_batches"].values()

    code = main(
        [
            "--runs-dir",
            str(runs),
            "experiment",
            "record",
            str(plan),
            "--condition",
            f"replay_only={batch_id}",
        ]
    )
    assert code == 0
    assert plan.read_bytes() == before
    _, result = RunReader(ArtifactStore(runs)).get_experiment("exp_000_baseline")
    assert result is not None
    assert result.metrics.verified_failure_count == 5


def test_the_retained_files_round_trip_byte_for_byte() -> None:
    """Loading and dumping the committed files changes nothing in them."""
    for name, model in (("experiment.json", ExperimentSpec), ("result.json", ExperimentResult)):
        raw = (RETAINED / name).read_text(encoding="utf-8")
        dumped = model.model_validate_json(raw).model_dump(mode="json")
        assert json.dumps(dumped, indent=2) + "\n" == raw


def test_the_retained_report_is_what_record_renders() -> None:
    spec = load_plan(json.loads((RETAINED / "experiment.json").read_text(encoding="utf-8")))
    result = ExperimentResult.model_validate_json((RETAINED / "result.json").read_text())
    assert (RETAINED / "report.md").read_text(encoding="utf-8") == render_experiment_markdown(
        spec, result
    )
