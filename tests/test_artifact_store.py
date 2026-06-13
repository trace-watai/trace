"""ArtifactStore + TraceRecorder: layout, JSON round-trips, JSONL traces."""

from __future__ import annotations

import json

import pytest

from trace_harness.runner.config import RunConfig
from trace_harness.tracing import artifact_store as names
from trace_harness.tracing.artifact_store import ArtifactStore
from trace_harness.tracing.events import TraceEventType
from trace_harness.tracing.recorder import TraceRecorder


def test_create_run_dir_and_json_round_trip(tmp_path):
    store = ArtifactStore(tmp_path / "runs")
    run_dir = store.create_run_dir("run_x")
    assert run_dir.is_dir()

    # Pydantic models and plain dicts both round-trip.
    config = RunConfig(task_id="t1", max_steps=3)
    store.write_json("run_x", names.RUN_CONFIG, config)
    loaded = store.read_json("run_x", names.RUN_CONFIG)
    assert loaded["task_id"] == "t1"
    assert loaded["max_steps"] == 3
    assert loaded["schema_version"] == "0.1.0"

    store.write_json("run_x", names.FINAL_STATE, {"orders": [], "refunds": []})
    assert store.read_json("run_x", names.FINAL_STATE) == {"orders": [], "refunds": []}

    # Files are valid, newline-terminated JSON on disk.
    raw = store.artifact_path("run_x", names.RUN_CONFIG).read_text()
    assert raw.endswith("\n")
    json.loads(raw)


def test_missing_artifact_raises_with_guidance(tmp_path):
    store = ArtifactStore(tmp_path / "runs")
    store.create_run_dir("run_x")
    with pytest.raises(FileNotFoundError, match="verifier_result.json"):
        store.read_json("run_x", names.VERIFIER_RESULT)


def test_trace_jsonl_round_trip(tmp_path):
    store = ArtifactStore(tmp_path / "runs")
    store.create_run_dir("run_x")
    recorder = TraceRecorder("run_x", jsonl_path=store.trace_path("run_x"))

    recorder.record(TraceEventType.RUN_STARTED, payload={"task_id": "t1"})
    recorder.record(TraceEventType.MODEL_ACTION, step_id=1, payload={"kind": "tool_call"})
    recorder.record(TraceEventType.RUN_FINISHED, payload={"status": "completed"})

    # Write-through: the file already has all three lines.
    lines = store.trace_path("run_x").read_text().strip().splitlines()
    assert len(lines) == 3

    reloaded = store.read_trace("run_x")
    assert [e.model_dump(mode="json") for e in reloaded] == [
        e.model_dump(mode="json") for e in recorder.events
    ]
    assert reloaded[0].event_id == "evt_000001"
    assert reloaded[1].step_id == 1
    assert reloaded[1].event_type is TraceEventType.MODEL_ACTION


def test_recorder_without_sink_keeps_events_in_memory():
    recorder = TraceRecorder("run_y")
    recorder.record(TraceEventType.RUN_STARTED)
    assert len(recorder.events) == 1


def test_corrupt_middle_line_raises_but_truncated_tail_is_dropped(tmp_path):
    """Corruption mid-trace raises; a half-written final line (hard kill
    mid write-through) is dropped so the surviving evidence stays usable."""
    store = ArtifactStore(tmp_path / "runs")
    store.create_run_dir("run_x")
    recorder = TraceRecorder("run_x", jsonl_path=store.trace_path("run_x"))
    recorder.record(TraceEventType.RUN_STARTED)
    recorder.record(TraceEventType.MODEL_ACTION, step_id=1)

    # Truncated tail: still readable, malformed line dropped.
    path = store.trace_path("run_x")
    path.write_text(path.read_text() + '{"half-writ')
    events = store.read_trace("run_x")
    assert [e.event_type for e in events] == [
        TraceEventType.RUN_STARTED,
        TraceEventType.MODEL_ACTION,
    ]

    # Corruption before the end: refuse loudly with the line number.
    path.write_text('{"not": "an event"}\n' + events[0].model_dump_json() + "\n")
    with pytest.raises(ValueError, match="trace.jsonl:1"):
        store.read_trace("run_x")


def test_for_run_path_and_list_runs(tmp_path):
    runs_dir = tmp_path / "runs"
    store = ArtifactStore(runs_dir)
    store.create_run_dir("run_20260101T000000Z_aaaa")
    store.create_run_dir("run_20260102T000000Z_bbbb")

    resolved_store, run_id = ArtifactStore.for_run_path(runs_dir / "run_20260101T000000Z_aaaa")
    assert run_id == "run_20260101T000000Z_aaaa"
    assert resolved_store.runs_dir == runs_dir

    assert store.list_runs() == [
        "run_20260101T000000Z_aaaa",
        "run_20260102T000000Z_bbbb",
    ]

    with pytest.raises(FileNotFoundError):
        ArtifactStore.for_run_path(runs_dir / "run_does_not_exist")


def test_write_json_overwrites_cleanly_with_no_temp_leftovers(tmp_path):
    store = ArtifactStore(tmp_path / "runs")
    store.create_run_dir("run_x")

    store.write_json("run_x", names.RUN_RESULT, {"status": "completed", "v": 1})
    store.write_json("run_x", names.RUN_RESULT, {"status": "completed", "v": 2})  # overwrite

    assert store.read_json("run_x", names.RUN_RESULT) == {"status": "completed", "v": 2}
    # The atomic temp file is renamed into place — nothing left behind.
    assert [p.name for p in store.run_dir("run_x").iterdir()] == [names.RUN_RESULT]


def test_failed_replace_preserves_existing_artifact(tmp_path, monkeypatch):
    """If the rename fails mid-write, the previous artifact is untouched and no
    temp file is left behind — the write is all-or-nothing, not a partial mix."""
    from trace_harness.tracing import artifact_store as module

    store = ArtifactStore(tmp_path / "runs")
    store.create_run_dir("run_x")
    store.write_json("run_x", names.RUN_RESULT, {"status": "completed", "v": 1})

    def boom(*_args, **_kwargs):
        raise OSError("simulated crash before rename")

    monkeypatch.setattr(module.os, "replace", boom)
    with pytest.raises(OSError, match="simulated crash"):
        store.write_json("run_x", names.RUN_RESULT, {"status": "FAIL", "v": 2})

    # Original artifact intact; the failed write neither corrupted nor replaced it.
    assert store.read_json("run_x", names.RUN_RESULT) == {"status": "completed", "v": 1}
    assert [p.name for p in store.run_dir("run_x").iterdir()] == [names.RUN_RESULT]
