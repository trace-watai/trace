"""TraceRecorder: append-only event recording with crash-safe JSONL output.

Why write-through
    Each event is flushed to disk the moment it is recorded, so a run that
    dies mid-flight still leaves a usable partial trace — preserving partial
    artifacts on failure is a hard requirement of the harness.

Why open-per-event
    Simplicity and durability beat throughput at MVP scale (a run is tens of
    events). If profiling ever says otherwise, switch to a held handle with
    explicit flush — behind this same interface.

# TODO(Samrath/tracing): event buffering + a storage backend interface when
# traces start flowing to an API/database instead of local JSONL.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from trace_harness.tracing.events import TraceEvent, TraceEventType


class TraceRecorder:
    """Records events for a single run, optionally writing JSONL as it goes."""

    def __init__(self, run_id: str, jsonl_path: Path | None = None):
        self.run_id = run_id
        self.jsonl_path = jsonl_path
        self._events: list[TraceEvent] = []
        self._next_seq = 1
        if jsonl_path is not None:
            jsonl_path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def events(self) -> list[TraceEvent]:
        return list(self._events)

    def record(
        self,
        event_type: TraceEventType,
        *,
        step_id: int | None = None,
        payload: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        parent_event_id: str | None = None,
    ) -> TraceEvent:
        """Create, store, and (if configured) immediately persist one event."""
        event = TraceEvent(
            event_id=f"evt_{self._next_seq:06d}",
            run_id=self.run_id,
            step_id=step_id,
            event_type=event_type,
            payload=payload or {},
            metadata=metadata or {},
            parent_event_id=parent_event_id,
        )
        self._next_seq += 1
        self._events.append(event)
        if self.jsonl_path is not None:
            with self.jsonl_path.open("a", encoding="utf-8") as fh:
                fh.write(event.model_dump_json() + "\n")
        return event

    @staticmethod
    def read_jsonl(path: Path) -> list[TraceEvent]:
        """Load a trace back from JSONL; the inverse of write-through recording.

        A malformed FINAL line is dropped with a warning instead of raising:
        a hard kill mid-write leaves exactly that shape, and refusing the
        whole trace would discard the surviving evidence (the
        partial-artifacts promise). Malformed lines anywhere else indicate
        corruption and still raise.
        """
        numbered = [
            (line_number, line)
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            )
            if line.strip()
        ]
        events: list[TraceEvent] = []
        for index, (line_number, line) in enumerate(numbered):
            try:
                events.append(TraceEvent.model_validate_json(line))
            except ValueError as exc:
                if index == len(numbered) - 1:
                    logging.getLogger(__name__).warning(
                        "dropping malformed final trace line %s:%d (truncated write?)",
                        path,
                        line_number,
                    )
                    break
                raise ValueError(f"invalid trace event at {path}:{line_number}: {exc}") from exc
        return events
