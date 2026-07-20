"""RunIndex: atomic runs-dir index, upsert/rebuild semantics, and the
guarded run-finalize hook. Mirrors tests/test_artifact_store.py style."""

from __future__ import annotations

import json
from datetime import datetime

from conftest import FAILURE_TASK_PATH, run_task_fixture
from trace_harness.tracing import artifact_store as names
from trace_harness.tracing.artifact_store import ArtifactStore
from trace_harness.tracing.run_index import RunIndex, RunIndexEntry


def _entry(run_id: str, *, task_id: str = "t1", status: str = "completed") -> RunIndexEntry:
    return RunIndexEntry(
        run_id=run_id,
        task_id=task_id,
        status=status,
        termination_reason="final_answer",
        steps_taken=3,
        started_at=datetime(2026, 1, 1, 0, 0, 0),
        finished_at=datetime(2026, 1, 1, 0, 0, 5),
    )


def test_upsert_round_trip_sorted_and_valid_json(tmp_path):
    store = ArtifactStore(tmp_path / "runs")
    store.upsert_index_entry(_entry("run_20260102T000000Z_bbbb"))
    store.upsert_index_entry(_entry("run_20260101T000000Z_aaaa"))

    index = store.read_index()
    assert [e.run_id for e in index.entries] == [
        "run_20260101T000000Z_aaaa",
        "run_20260102T000000Z_bbbb",
    ]
    assert index.schema_version == "0.1.0"

    # On disk at the runs-dir root, valid newline-terminated JSON.
    raw = store.index_path().read_text()
    assert store.index_path().parent == store.runs_dir
    assert raw.endswith("\n")
    json.loads(raw)


def test_upsert_replaces_not_duplicates(tmp_path):
    store = ArtifactStore(tmp_path / "runs")
    store.upsert_index_entry(_entry("run_x", status="completed"))
    store.upsert_index_entry(_entry("run_x", status="error"))

    index = store.read_index()
    assert len(index.entries) == 1
    assert index.entries[0].status == "error"  # latest wins


def test_missing_index_reads_empty(tmp_path):
    store = ArtifactStore(tmp_path / "runs")
    assert store.read_index() == RunIndex(entries=[])


def test_corrupt_index_self_heals_from_run_results(tmp_path):
    """A hand-edited/corrupt index must not lose history: the next upsert
    rebuilds from the run_result.json files still on disk."""
    store = ArtifactStore(tmp_path / "runs")
    # A finished run exists on disk (has run_result.json) but is missing from
    # the index, which has been corrupted.
    store.create_run_dir("run_old")
    store.write_json("run_old", names.RUN_RESULT, _entry("run_old").model_dump(mode="json"))
    store.index_path().write_text("{ this is not valid json")

    store.upsert_index_entry(_entry("run_new"))

    run_ids = {e.run_id for e in store.read_index().entries}
    assert run_ids == {"run_old", "run_new"}  # old history recovered, new added


def test_rebuild_skips_runs_without_result(tmp_path):
    store = ArtifactStore(tmp_path / "runs")
    store.create_run_dir("run_finished")
    store.write_json(
        "run_finished", names.RUN_RESULT, _entry("run_finished").model_dump(mode="json")
    )
    store.create_run_dir("run_crashed_before_result")  # no run_result.json

    index = store.rebuild_index()
    assert [e.run_id for e in index.entries] == ["run_finished"]


def test_index_file_excluded_from_list_runs(tmp_path):
    store = ArtifactStore(tmp_path / "runs")
    store.create_run_dir("run_a")
    store.upsert_index_entry(_entry("run_a"))
    # index.json is a file at the runs-dir root, not a run directory.
    assert store.list_runs() == ["run_a"]


def test_fixture_run_writes_matching_index_entry(tmp_path):
    run = run_task_fixture(FAILURE_TASK_PATH, tmp_path / "runs")
    index = run.store.read_index()

    assert len(index.entries) == 1
    entry = index.entries[0]
    assert entry.run_id == run.result.run_id
    assert entry.task_id == run.result.task_id
    assert entry.status == run.result.status
    assert entry.termination_reason == run.result.termination_reason
    assert entry.steps_taken == run.result.steps_taken


def test_index_failure_cannot_break_a_run(tmp_path, monkeypatch):
    """The index is a derived convenience: if upsert raises, the run still
    returns its result and run_result.json is intact (guarded hook)."""

    def boom(*_args, **_kwargs):
        raise OSError("index disk full")

    monkeypatch.setattr(ArtifactStore, "upsert_index_entry", boom)
    run = run_task_fixture(FAILURE_TASK_PATH, tmp_path / "runs")
    assert run.result.run_id  # run completed and returned
    assert run.store.exists(run.result.run_id, names.RUN_RESULT)
