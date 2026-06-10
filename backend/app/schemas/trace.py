"""The TraceRun aggregate plus JSONL (de)serialization helpers.

``TraceRun`` is the logical whole of one run. Physical storage splits it across
``runs/{run_id}/`` files — that file layout and the durable writer/reader are TRA-8.
This module provides the in-memory contract and the lossless JSONL round-trip for
the trajectory, which is what TRA-22 must guarantee.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .events import TraceEvent, TraceEventAdapter
from .run import AgentConfig, RunMetadata, StateSnapshot, TaskSnapshot
from .verification import AttributionResult, VerifierResult


def events_to_jsonl(events: list[TraceEvent]) -> str:
    """Serialize events to JSONL (one event per line), ordered by step_id."""
    ordered = sorted(events, key=lambda e: e.step_id)
    return "\n".join(TraceEventAdapter.dump_json(e).decode("utf-8") for e in ordered)


def events_from_jsonl(text: str) -> list[TraceEvent]:
    """Parse a JSONL trajectory back into typed events via the discriminated union."""
    return [TraceEventAdapter.validate_json(line) for line in text.splitlines() if line.strip()]


class TraceRun(BaseModel):
    """One complete run: metadata + task snapshot + agent config + initial/final
    state + trajectory + (optional) verifier/attribution results."""

    model_config = ConfigDict(extra="forbid")

    metadata: RunMetadata
    task_snapshot: TaskSnapshot
    agent_config: AgentConfig
    initial_state: StateSnapshot | None = None
    final_state: StateSnapshot | None = None
    events: list[TraceEvent] = Field(default_factory=list)
    verifier_result: VerifierResult | None = None
    attribution: AttributionResult | None = None

    def ordered_events(self) -> list[TraceEvent]:
        return sorted(self.events, key=lambda e: e.step_id)

    def validate_ordering(self) -> None:
        """Raise ValueError unless step_ids are unique and strictly increasing in 0..n-1.

        Deterministic ordering + stable, gapless ids are TRA-22 acceptance criteria.
        """
        ids = [e.step_id for e in self.events]
        if ids != list(range(len(ids))):
            raise ValueError(f"step_ids must be unique and gapless 0..n-1 in emission order, got {ids}")

    def trace_jsonl(self) -> str:
        return events_to_jsonl(self.events)
