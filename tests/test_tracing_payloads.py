"""Tests for typed per-event-type payload models and parent_event_id.

All offline — no keys, no network, no filesystem side-effects.
"""

from __future__ import annotations

import pytest

from trace_harness.tracing.events import (
    TRACE_SCHEMA_VERSION,
    TraceEvent,
    TraceEventType,
)
from trace_harness.tracing.payloads import (
    PAYLOAD_TYPES,
    ErrorPayload,
    FinalAnswerPayload,
    ModelActionPayload,
    ModelPromptPayload,
    ModelResponsePayload,
    RetrievalResultPayload,
    RunFinishedPayload,
    RunStartedPayload,
    StateSnapshotPayload,
    TaskLoadedPayload,
    ToolCallExecutedPayload,
    ToolCallRequestedPayload,
    ToolCallValidatedPayload,
    ToolObservationPayload,
)
from trace_harness.tracing.recorder import TraceRecorder

# --- Schema version ---


def test_schema_version_bumped_to_0_2_0() -> None:
    assert TRACE_SCHEMA_VERSION == "0.2.0"


def test_new_event_carries_schema_version() -> None:
    recorder = TraceRecorder("run_test_001")
    event = recorder.record(
        TraceEventType.RUN_FINISHED,
        payload={"status": "completed", "termination_reason": "final_answer", "steps_taken": 2},
    )
    assert event.schema_version == "0.2.0"


# --- PAYLOAD_TYPES coverage ---


def test_all_event_types_have_payload_model() -> None:
    for et in TraceEventType:
        assert et in PAYLOAD_TYPES, f"missing payload model for {et!r}"


# --- Per-type validation ---


def test_run_started_payload_validates() -> None:
    p = RunStartedPayload.model_validate(
        {
            "task_id": "t1",
            "provider": "fixture",
            "model": None,
            "max_steps": 10,
            "timeout_seconds": 30.0,
            "prompt_version": "v0",
        }
    )
    assert p.task_id == "t1"
    assert p.provider == "fixture"
    assert p.max_steps == 10


def test_task_loaded_payload_validates() -> None:
    p = TaskLoadedPayload.model_validate({"task": {"task_id": "t1"}})
    assert p.task["task_id"] == "t1"


def test_state_snapshot_payload_validates() -> None:
    p = StateSnapshotPayload.model_validate({"phase": "initial", "state": {}})
    assert p.phase == "initial"


def test_model_prompt_payload_validates() -> None:
    p = ModelPromptPayload.model_validate({"transcript_length": 2, "new_messages": []})
    assert p.transcript_length == 2


def test_model_response_payload_validates_empty() -> None:
    p = ModelResponsePayload.model_validate({})
    assert p.raw is None


def test_model_action_payload_validates() -> None:
    p = ModelActionPayload.model_validate(
        {
            "kind": "tool_call",
            "tool_call": {"tool_name": "lookup", "arguments": {}},
            "final_answer": None,
            "reasoning": None,
        }
    )
    assert p.kind == "tool_call"


def test_tool_call_requested_payload_validates() -> None:
    p = ToolCallRequestedPayload.model_validate(
        {"tool_name": "lookup_policy", "arguments": {"policy_id": "P1"}}
    )
    assert p.tool_name == "lookup_policy"
    assert p.arguments == {"policy_id": "P1"}


def test_tool_call_validated_payload_validates() -> None:
    p = ToolCallValidatedPayload.model_validate(
        {"tool_name": "lookup_policy", "valid": True, "error": None}
    )
    assert p.valid is True


def test_tool_call_executed_payload_validates() -> None:
    p = ToolCallExecutedPayload.model_validate(
        {
            "tool_name": "lookup_policy",
            "arguments": {},
            "status": "ok",
            "side_effect": None,
            "error": None,
        }
    )
    assert p.status == "ok"


def test_retrieval_result_payload_validates() -> None:
    p = RetrievalResultPayload.model_validate(
        {"query": "refund policy", "result_count": 1, "results": [{"doc_id": "d1"}]}
    )
    assert p.result_count == 1
    assert p.query == "refund policy"


def test_tool_observation_payload_validates() -> None:
    p = ToolObservationPayload.model_validate(
        {"tool_name": "lookup_policy", "status": "ok", "result": {"text": "..."}, "error": None}
    )
    assert p.tool_name == "lookup_policy"


def test_final_answer_payload_validates() -> None:
    p = FinalAnswerPayload.model_validate({"final_answer": "No refund."})
    assert p.final_answer == "No refund."


def test_run_finished_payload_validates() -> None:
    p = RunFinishedPayload.model_validate(
        {"status": "completed", "termination_reason": "final_answer", "steps_taken": 3}
    )
    assert p.steps_taken == 3


def test_error_payload_validates_without_traceback() -> None:
    p = ErrorPayload.model_validate({"error": "oops", "kind": "internal_error"})
    assert p.traceback is None


def test_error_payload_validates_with_traceback() -> None:
    p = ErrorPayload.model_validate(
        {"error": "oops", "kind": "internal_error", "traceback": "Traceback ..."}
    )
    assert "Traceback" in p.traceback


# --- extra="ignore" forward compatibility ---


def test_unknown_keys_are_silently_ignored() -> None:
    p = RunStartedPayload.model_validate(
        {
            "task_id": "t1",
            "provider": "fixture",
            "max_steps": 5,
            "timeout_seconds": 10.0,
            "prompt_version": "v0",
            "future_field_not_yet_defined": "ignored",
        }
    )
    assert p.task_id == "t1"
    assert not hasattr(p, "future_field_not_yet_defined")


# --- typed_payload property on TraceEvent ---


def _make_event(event_type: TraceEventType, payload: dict) -> TraceEvent:
    recorder = TraceRecorder("run_test_001")
    return recorder.record(event_type, payload=payload)


def test_typed_payload_returns_correct_type_for_run_started() -> None:
    ev = _make_event(
        TraceEventType.RUN_STARTED,
        {
            "task_id": "t1",
            "provider": "fixture",
            "model": None,
            "max_steps": 5,
            "timeout_seconds": 10.0,
            "prompt_version": "v0",
        },
    )
    p = ev.typed_payload
    assert isinstance(p, RunStartedPayload)
    assert p.task_id == "t1"


def test_typed_payload_returns_correct_type_for_tool_observation() -> None:
    ev = _make_event(
        TraceEventType.TOOL_OBSERVATION,
        {"tool_name": "check_balance", "status": "ok", "result": 100, "error": None},
    )
    p = ev.typed_payload
    assert isinstance(p, ToolObservationPayload)
    assert p.status == "ok"


def test_typed_payload_returns_correct_type_for_error() -> None:
    ev = _make_event(
        TraceEventType.ERROR,
        {"error": "boom", "kind": "internal_error"},
    )
    p = ev.typed_payload
    assert isinstance(p, ErrorPayload)
    assert p.error == "boom"


@pytest.mark.parametrize("event_type", list(TraceEventType))
def test_typed_payload_class_registered_for_all_event_types(event_type: TraceEventType) -> None:
    """Every event type has an entry in PAYLOAD_TYPES so typed_payload never returns None."""
    from trace_harness.tracing.payloads import PAYLOAD_TYPES

    assert event_type in PAYLOAD_TYPES


# --- parent_event_id field ---


def test_parent_event_id_defaults_to_none() -> None:
    recorder = TraceRecorder("run_test_002")
    ev = recorder.record(TraceEventType.RUN_STARTED, payload={})
    assert ev.parent_event_id is None


def test_parent_event_id_round_trips_through_json() -> None:
    recorder = TraceRecorder("run_test_003")
    parent = recorder.record(
        TraceEventType.TOOL_CALL_REQUESTED,
        payload={"tool_name": "x", "arguments": {}},
    )
    child = recorder.record(
        TraceEventType.TOOL_CALL_VALIDATED,
        payload={"tool_name": "x", "valid": True, "error": None},
        parent_event_id=parent.event_id,
    )
    assert child.parent_event_id == parent.event_id

    # Round-trip through JSON
    restored = TraceEvent.model_validate_json(child.model_dump_json())
    assert restored.parent_event_id == parent.event_id


def test_parent_event_id_preserved_in_jsonl(tmp_path) -> None:
    jsonl = tmp_path / "trace.jsonl"
    recorder = TraceRecorder("run_test_004", jsonl_path=jsonl)

    parent = recorder.record(
        TraceEventType.TOOL_CALL_REQUESTED,
        step_id=1,
        payload={"tool_name": "lookup", "arguments": {}},
    )
    recorder.record(
        TraceEventType.TOOL_CALL_VALIDATED,
        step_id=1,
        payload={"tool_name": "lookup", "valid": True, "error": None},
        parent_event_id=parent.event_id,
    )
    recorder.record(
        TraceEventType.TOOL_CALL_EXECUTED,
        step_id=1,
        payload={
            "tool_name": "lookup",
            "arguments": {},
            "status": "ok",
            "side_effect": None,
            "error": None,
        },
        parent_event_id=parent.event_id,
    )
    recorder.record(
        TraceEventType.RETRIEVAL_RESULT,
        step_id=1,
        payload={"query": "policy", "result_count": 0, "results": []},
        parent_event_id=parent.event_id,
    )
    recorder.record(
        TraceEventType.TOOL_OBSERVATION,
        step_id=1,
        payload={"tool_name": "lookup", "status": "ok", "result": {}, "error": None},
        parent_event_id=parent.event_id,
    )

    events = TraceRecorder.read_jsonl(jsonl)
    child_types = {
        TraceEventType.TOOL_CALL_VALIDATED,
        TraceEventType.TOOL_CALL_EXECUTED,
        TraceEventType.RETRIEVAL_RESULT,
        TraceEventType.TOOL_OBSERVATION,
    }
    for ev in events:
        if ev.event_type in child_types:
            assert ev.parent_event_id == parent.event_id, (
                f"{ev.event_type} should have parent_event_id set"
            )
        else:
            assert ev.parent_event_id is None


# --- Runner wires parent_event_id for tool-call chains ---


def test_runner_wires_tool_chain_parent_event_ids(tmp_path) -> None:
    """Runner emits TOOL_CALL_VALIDATED/EXECUTED/OBSERVATION with parent pointing to REQUESTED."""
    from conftest import FAILURE_TASK_PATH
    from trace_harness.cli import main
    from trace_harness.tracing.artifact_store import ArtifactStore
    from trace_harness.tracing.events import TraceEventType

    runs_dir = tmp_path / "runs"
    rc = main(["--runs-dir", str(runs_dir), "run-fixture", str(FAILURE_TASK_PATH)])
    assert rc == 0

    store = ArtifactStore(runs_dir)
    run_ids = store.list_runs()
    assert run_ids
    events = store.read_trace(run_ids[0])

    requested = {
        ev.event_id: ev for ev in events if ev.event_type == TraceEventType.TOOL_CALL_REQUESTED
    }
    child_types = {
        TraceEventType.TOOL_CALL_VALIDATED,
        TraceEventType.TOOL_CALL_EXECUTED,
        TraceEventType.RETRIEVAL_RESULT,
        TraceEventType.TOOL_OBSERVATION,
    }
    children = [ev for ev in events if ev.event_type in child_types]
    assert children, "expected at least one tool call in the failure fixture"
    for ev in children:
        assert ev.parent_event_id is not None, f"{ev.event_type} missing parent_event_id"
        assert ev.parent_event_id in requested, (
            f"{ev.event_type} parent {ev.parent_event_id!r} not a TOOL_CALL_REQUESTED event_id"
        )
