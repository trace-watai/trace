"""Trajectory events — the lines of trace.jsonl.

A run's trajectory is an ordered list of events. Each event is one JSON object on
one line. Events form a discriminated union keyed on ``step_type``.

ORDERING & IDENTITY CONVENTION (see README.md):
- ``step_id``: 0-indexed integer, strictly increasing by emission order. It is the
  *canonical ordering key* — ordering never depends on timestamps (which are
  optional and may collide). Step ids are stable across reloads.
- ``parent_step_id``: links a derived event to its origin (e.g. a ``tool_observation``
  to the ``tool_call`` it answers), giving the trace its parent relationships.

DESIGN RULES (Notion → Trace Schema & Data Formats):
- Tool *arguments* are logged BEFORE execution (ToolCallEvent); *observations* AFTER
  (ToolObservationEvent). The two are distinct events.
- Raw observations and summarized observations are kept separate.
- timestamp / cost / latency_ms are optional but supported from day one.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from .common import MessageRole
from .verification import AttributionResult, VerifierResult


class _BaseEvent(BaseModel):
    """Fields common to every trajectory event. ``extra='forbid'`` so malformed
    events fail validation loudly (TRA-8 acceptance: useful errors on bad events)."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    step_id: int = Field(..., ge=0, description="0-indexed, strictly increasing. Canonical ordering key.")
    parent_step_id: int | None = Field(None, description="step_id this event derives from.")
    timestamp: datetime | None = None
    cost: float | None = Field(None, description="Optional cost (tokens or $) attributed to this step.")
    latency_ms: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RetrievedDoc(BaseModel):
    model_config = ConfigDict(extra="allow")

    doc_id: str
    title: str | None = None
    score: float | None = None
    rank: int | None = None
    is_current: bool | None = Field(
        None, description="Current vs deprecated source — central to stale-policy failures."
    )
    span: str | None = Field(None, description="Cited text span used as grounding/evidence.")


class MessageEvent(_BaseEvent):
    step_type: Literal["message"] = "message"
    role: MessageRole
    content: str
    summary: str | None = None


class ReasoningEvent(_BaseEvent):
    step_type: Literal["reasoning"] = "reasoning"
    summary: str = Field(..., description="Summarized reasoning. Store raw hidden CoT only if exposed and allowed.")
    raw: str | None = None


class RetrievalEvent(_BaseEvent):
    step_type: Literal["retrieval"] = "retrieval"
    query: str
    retrieved: list[RetrievedDoc] = Field(default_factory=list)


class ToolCallEvent(_BaseEvent):
    """Logged BEFORE the tool executes."""

    step_type: Literal["tool_call"] = "tool_call"
    tool_name: str
    tool_args: dict[str, Any] = Field(default_factory=dict)
    call_id: str | None = Field(None, description="Correlates with the matching tool_observation.")


class ToolObservationEvent(_BaseEvent):
    """Logged AFTER the tool executes. ``parent_step_id`` points at the ToolCallEvent."""

    step_type: Literal["tool_observation"] = "tool_observation"
    tool_name: str
    call_id: str | None = None
    observation: Any = Field(None, description="Raw observation from the tool. Keep raw; summary is separate.")
    observation_summary: str | None = None
    ok: bool | None = None
    error: str | None = None


class StateSnapshotEvent(_BaseEvent):
    step_type: Literal["state_snapshot"] = "state_snapshot"
    label: str = Field(..., description="'initial' | 'final' | 'intermediate'.")
    state: dict[str, Any] = Field(default_factory=dict)


class VerifierEvent(_BaseEvent):
    step_type: Literal["verifier"] = "verifier"
    result: VerifierResult


class AttributionEvent(_BaseEvent):
    step_type: Literal["attribution"] = "attribution"
    result: AttributionResult


class FinalAnswerEvent(_BaseEvent):
    step_type: Literal["final_answer"] = "final_answer"
    content: str
    structured: dict[str, Any] | None = None


class ErrorEvent(_BaseEvent):
    step_type: Literal["error"] = "error"
    error_type: str
    message: str
    recoverable: bool | None = None


TraceEvent = Annotated[
    Union[
        MessageEvent,
        ReasoningEvent,
        RetrievalEvent,
        ToolCallEvent,
        ToolObservationEvent,
        StateSnapshotEvent,
        VerifierEvent,
        AttributionEvent,
        FinalAnswerEvent,
        ErrorEvent,
    ],
    Field(discriminator="step_type"),
]

# Use to parse a single event of unknown subtype from a dict or JSON line.
TraceEventAdapter: TypeAdapter[TraceEvent] = TypeAdapter(TraceEvent)
