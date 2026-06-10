"""Shared constants and enums for the TRACE trace/run schema.

TRA-22 — canonical trace & run schemas. Keep this module dependency-light
(stdlib + enum only) so every other schema module can import it freely.
"""

from __future__ import annotations

from enum import Enum

# Bump on any breaking change to the trace/run contract. Embedded in RunMetadata
# and exported alongside JSON Schemas so stored artifacts are self-describing.
TRACE_SCHEMA_VERSION = "0.1.0"


class StepType(str, Enum):
    """Discriminator values for trajectory events (see events.py)."""

    message = "message"
    reasoning = "reasoning"
    retrieval = "retrieval"
    tool_call = "tool_call"
    tool_observation = "tool_observation"
    state_snapshot = "state_snapshot"
    verifier = "verifier"
    attribution = "attribution"
    final_answer = "final_answer"
    error = "error"


class RunStatus(str, Enum):
    """Lifecycle of a run. NOTE: this is execution status, NOT task pass/fail —
    correctness is decided by the verifier (see verification.py)."""

    running = "running"
    completed = "completed"  # agent finished normally (may still have failed the task)
    error = "error"  # crashed, aborted, or hit a step/time budget before finishing


class WorkflowType(str, Enum):
    tool_api = "tool_api"
    rag = "rag"


class MessageRole(str, Enum):
    system = "system"
    user = "user"
    assistant = "assistant"
    tool = "tool"


class StateLabel(str, Enum):
    initial = "initial"
    final = "final"
    intermediate = "intermediate"
