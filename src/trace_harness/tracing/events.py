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

Intended evolution (documented now so nobody is surprised)
    - MVP uses one generic ``TraceEvent`` with dict payloads; the planned
      next step is typed payload models per event type (discriminated by
      ``event_type``) so consumers stop string-indexing dicts.
    - Nested spans (sub-agent calls, retries) will need ``parent_event_id``.

# TODO(Samrath/tracing): add parent event IDs when nested spans are
# introduced; add typed payload models per event type after the dashboard's
# first read pass tells us which fields it actually needs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

TRACE_SCHEMA_VERSION = "0.1.0"


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
