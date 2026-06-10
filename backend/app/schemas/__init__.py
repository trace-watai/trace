"""TRACE canonical trace & run schemas (TRA-22).

Public contract consumed by the runner (TRA-7), storage (TRA-8), verifiers
(TRA-16), attribution/judge (TRA-10), and the dashboard data contract (TRA-27).
"""

from __future__ import annotations

from .common import (
    TRACE_SCHEMA_VERSION,
    MessageRole,
    RunStatus,
    StateLabel,
    StepType,
    WorkflowType,
)
from .events import (
    AttributionEvent,
    ErrorEvent,
    FinalAnswerEvent,
    MessageEvent,
    ReasoningEvent,
    RetrievalEvent,
    RetrievedDoc,
    StateSnapshotEvent,
    ToolCallEvent,
    ToolObservationEvent,
    TraceEvent,
    TraceEventAdapter,
    VerifierEvent,
)
from .run import AgentConfig, RunMetadata, StateSnapshot, TaskSnapshot
from .trace import TraceRun, events_from_jsonl, events_to_jsonl
from .verification import AttributionResult, Evidence, VerifierCheck, VerifierResult

__all__ = [
    "TRACE_SCHEMA_VERSION",
    # enums
    "StepType",
    "RunStatus",
    "WorkflowType",
    "MessageRole",
    "StateLabel",
    # run-level
    "RunMetadata",
    "AgentConfig",
    "TaskSnapshot",
    "StateSnapshot",
    # events
    "TraceEvent",
    "TraceEventAdapter",
    "MessageEvent",
    "ReasoningEvent",
    "RetrievalEvent",
    "RetrievedDoc",
    "ToolCallEvent",
    "ToolObservationEvent",
    "StateSnapshotEvent",
    "VerifierEvent",
    "AttributionEvent",
    "FinalAnswerEvent",
    "ErrorEvent",
    # verification
    "VerifierResult",
    "VerifierCheck",
    "AttributionResult",
    "Evidence",
    # aggregate + jsonl
    "TraceRun",
    "events_to_jsonl",
    "events_from_jsonl",
]
