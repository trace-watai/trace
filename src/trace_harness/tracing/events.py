"""The trace event schema: the structured log of what happened during a run.

A trace is an append-only sequence of :class:`TraceEvent` records, one JSON
object per line in ``runs/{run_id}/trace.jsonl``. Everything downstream —
verifiers, attribution, failure bundles, the dashboard — consumes traces,
so this schema is a *contract*: change it deliberately, bump
``TRACE_SCHEMA_VERSION``, and coordinate with every consumer.

Step numbering
    ``step_id`` counts agent decision steps starting at 1. All events caused
    by one decision (the prompt, the action, the tool call, the observation)
    share that step's id. Run-level events (run_started, state snapshots,
    run_finished) have ``step_id=None``.

Parent events
    ``parent_event_id`` links child events to their parent within the same run:
    e.g. TOOL_CALL_VALIDATED / TOOL_CALL_EXECUTED / TOOL_OBSERVATION all point
    to the originating TOOL_CALL_REQUESTED event. ``None`` means top-level.

Typed payloads
    ``payload`` is the raw dict — the source of truth, always round-trips
    through JSONL. ``typed_payload`` is a lazily-built read-only view that
    deserializes ``payload`` into the per-event-type Pydantic model from
    :mod:`trace_harness.tracing.payloads`, using ``extra="ignore"`` so traces
    written by newer runners do not break older readers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from trace_harness.tracing.payloads import TracePayload

TRACE_SCHEMA_VERSION = "0.2.0"


class TraceEventType(StrEnum):
    """Every kind of event a run may emit.

    MVP runs emit a subset (the fixture adapter produces no separate
    ``model_response``; that type is reserved for real provider adapters
    whose raw response differs from the normalized action).
    """

    RUN_STARTED = "run_started"
    TASK_LOADED = "task_loaded"
    STATE_SNAPSHOT = "state_snapshot"
    MODEL_PROMPT = "model_prompt"
    MODEL_RESPONSE = "model_response"
    MODEL_ACTION = "model_action"
    TOOL_CALL_REQUESTED = "tool_call_requested"
    TOOL_CALL_VALIDATED = "tool_call_validated"
    TOOL_CALL_EXECUTED = "tool_call_executed"
    TOOL_OBSERVATION = "tool_observation"
    RETRIEVAL_RESULT = "retrieval_result"
    FINAL_ANSWER = "final_answer"
    RUN_FINISHED = "run_finished"
    ERROR = "error"


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


class TraceEvent(BaseModel):
    """One structured event in a run's trace."""

    schema_version: str = TRACE_SCHEMA_VERSION
    event_id: str
    run_id: str
    step_id: int | None = None
    event_type: TraceEventType
    timestamp: datetime = Field(default_factory=utc_now)
    payload: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    parent_event_id: str | None = None

    @property
    def typed_payload(self) -> TracePayload | None:
        """Payload deserialized into the per-event-type model (read-only view).

        Returns ``None`` if no model is registered for this ``event_type``.
        Uses ``extra="ignore"`` so unknown fields from newer runners are
        silently dropped rather than raising.
        """
        from trace_harness.tracing.payloads import PAYLOAD_TYPES  # local to avoid circular import

        cls = PAYLOAD_TYPES.get(self.event_type)
        return cls.model_validate(self.payload) if cls else None
