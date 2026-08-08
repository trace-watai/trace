"""Typed payload models for each TraceEvent event_type.

Each model captures the fields the runner emits for that event type.
``extra="ignore"`` makes them forward-compatible: new fields added by a newer
runner version are silently dropped from the typed view, so old readers do not
fail on new traces.

Usage::

    event = trace[5]
    if event.event_type == TraceEventType.TOOL_CALL_REQUESTED:
        p = event.typed_payload  # ToolCallRequestedPayload
        print(p.tool_name, p.arguments)

The raw ``event.payload`` dict is always the source of truth; typed models are
a read-only view built from it via ``TraceEvent.typed_payload``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from trace_harness.tracing.events import TraceEventType


class _IgnoreExtra(BaseModel, extra="ignore"):
    """Base that silently ignores unknown payload fields for forward compat."""


class RunStartedPayload(_IgnoreExtra):
    task_id: str
    provider: str
    model: str | None = None
    max_steps: int
    timeout_seconds: float
    prompt_version: str


class TaskLoadedPayload(_IgnoreExtra):
    task: dict[str, Any]


class StateSnapshotPayload(_IgnoreExtra):
    phase: str  # "initial" | "final"
    state: dict[str, Any]


class ModelPromptPayload(_IgnoreExtra):
    transcript_length: int
    new_messages: list[dict[str, Any]]


class ModelResponsePayload(_IgnoreExtra):
    raw: dict[str, Any] | None = None


class ModelActionPayload(_IgnoreExtra):
    kind: str
    tool_call: dict[str, Any] | None = None
    final_answer: str | None = None
    reasoning: str | None = None


class ToolCallRequestedPayload(_IgnoreExtra):
    tool_name: str
    arguments: dict[str, Any]


class ToolCallValidatedPayload(_IgnoreExtra):
    tool_name: str
    valid: bool
    error: str | None = None


class ToolCallExecutedPayload(_IgnoreExtra):
    tool_name: str
    arguments: dict[str, Any]
    status: str
    side_effect: str | None = None
    error: str | None = None


class RetrievalResultItem(_IgnoreExtra):
    doc_id: str
    status: str
    title: str | None = None
    score: float | None = None
    source: str | None = None


class RetrievalResultPayload(_IgnoreExtra):
    query: str | None = None
    result_count: int
    results: list[RetrievalResultItem]


class ToolObservationPayload(_IgnoreExtra):
    tool_name: str
    status: str
    result: Any = None
    error: str | None = None


class FinalAnswerPayload(_IgnoreExtra):
    final_answer: str


class RunFinishedPayload(_IgnoreExtra):
    status: str
    termination_reason: str
    steps_taken: int


class ErrorPayload(_IgnoreExtra):
    error: str
    kind: str
    traceback: str | None = None


TracePayload = (
    RunStartedPayload
    | TaskLoadedPayload
    | StateSnapshotPayload
    | ModelPromptPayload
    | ModelResponsePayload
    | ModelActionPayload
    | ToolCallRequestedPayload
    | ToolCallValidatedPayload
    | ToolCallExecutedPayload
    | RetrievalResultPayload
    | ToolObservationPayload
    | FinalAnswerPayload
    | RunFinishedPayload
    | ErrorPayload
)


PAYLOAD_TYPES: dict[TraceEventType, type[_IgnoreExtra]] = {
    TraceEventType.RUN_STARTED: RunStartedPayload,
    TraceEventType.TASK_LOADED: TaskLoadedPayload,
    TraceEventType.STATE_SNAPSHOT: StateSnapshotPayload,
    TraceEventType.MODEL_PROMPT: ModelPromptPayload,
    TraceEventType.MODEL_RESPONSE: ModelResponsePayload,
    TraceEventType.MODEL_ACTION: ModelActionPayload,
    TraceEventType.TOOL_CALL_REQUESTED: ToolCallRequestedPayload,
    TraceEventType.TOOL_CALL_VALIDATED: ToolCallValidatedPayload,
    TraceEventType.TOOL_CALL_EXECUTED: ToolCallExecutedPayload,
    TraceEventType.RETRIEVAL_RESULT: RetrievalResultPayload,
    TraceEventType.TOOL_OBSERVATION: ToolObservationPayload,
    TraceEventType.FINAL_ANSWER: FinalAnswerPayload,
    TraceEventType.RUN_FINISHED: RunFinishedPayload,
    TraceEventType.ERROR: ErrorPayload,
}
