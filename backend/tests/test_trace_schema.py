"""TRA-22 acceptance tests: lossless round-trip, deterministic ordering, stable ids,
discriminated-union parsing, and JSON Schema export."""

from __future__ import annotations

import json

import pytest

from app.schemas import (
    TRACE_SCHEMA_VERSION,
    AttributionEvent,
    MessageEvent,
    ToolCallEvent,
    ToolObservationEvent,
    TraceEventAdapter,
    TraceRun,
    VerifierEvent,
    events_from_jsonl,
    events_to_jsonl,
)
from app.schemas.examples import build_sample_refund_run
from app.schemas.export import build_schemas


@pytest.fixture()
def run() -> TraceRun:
    return build_sample_refund_run()


def test_sample_run_validates_and_orders(run: TraceRun) -> None:
    run.validate_ordering()  # raises if not gapless 0..n-1
    ids = [e.step_id for e in run.events]
    assert ids == sorted(ids), "events must already be in step_id order"
    assert run.metadata.step_count == len(run.events)
    assert run.metadata.schema_version == TRACE_SCHEMA_VERSION


def test_jsonl_round_trip_is_lossless(run: TraceRun) -> None:
    jsonl = events_to_jsonl(run.events)
    reparsed = events_from_jsonl(jsonl)
    # Compare via canonical model dumps so types (datetime, enums) normalize identically.
    assert [e.model_dump(mode="json") for e in reparsed] == [
        e.model_dump(mode="json") for e in run.ordered_events()
    ]
    # One JSON object per line, nothing dropped.
    assert len(jsonl.splitlines()) == len(run.events)


def test_full_run_round_trip_is_lossless(run: TraceRun) -> None:
    blob = run.model_dump_json()
    assert TraceRun.model_validate_json(blob).model_dump(mode="json") == run.model_dump(mode="json")


def test_discriminator_selects_correct_subtype() -> None:
    for raw, expected in [
        ({"run_id": "r", "step_id": 0, "step_type": "message", "role": "user", "content": "hi"}, MessageEvent),
        (
            {"run_id": "r", "step_id": 1, "step_type": "tool_call", "tool_name": "get_order", "tool_args": {}},
            ToolCallEvent,
        ),
        (
            {"run_id": "r", "step_id": 2, "step_type": "tool_observation", "tool_name": "get_order", "observation": {}},
            ToolObservationEvent,
        ),
    ]:
        assert isinstance(TraceEventAdapter.validate_python(raw), expected)


def test_malformed_event_is_rejected() -> None:
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        # unknown field on a strict event must fail (extra="forbid")
        TraceEventAdapter.validate_python(
            {"run_id": "r", "step_id": 0, "step_type": "message", "role": "user", "content": "hi", "bogus": 1}
        )
    with pytest.raises(pydantic.ValidationError):
        # missing required discriminator
        TraceEventAdapter.validate_python({"run_id": "r", "step_id": 0})


def test_parent_relationships_present(run: TraceRun) -> None:
    by_id = {e.step_id: e for e in run.events}
    obs = [e for e in run.events if isinstance(e, ToolObservationEvent)]
    assert obs, "sample must contain tool observations"
    for o in obs:
        assert o.parent_step_id is not None
        assert isinstance(by_id[o.parent_step_id], ToolCallEvent), "observation must point at its tool_call"


def test_verifier_and_attribution_carried_in_trajectory(run: TraceRun) -> None:
    assert any(isinstance(e, VerifierEvent) for e in run.events)
    assert any(isinstance(e, AttributionEvent) for e in run.events)
    # Evidence-first: attribution agrees the verifier failed.
    assert run.verifier_result is not None and run.verifier_result.verifier_passed is False
    assert run.attribution is not None and run.attribution.task_success is False


def test_json_schema_export_is_wellformed() -> None:
    schemas = build_schemas()
    for name in ("trace_run", "trace_event", "run_metadata", "verifier_result", "attribution_result"):
        assert name in schemas
        # must be serializable JSON Schema dicts
        json.dumps(schemas[name])
