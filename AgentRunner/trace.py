"""TRACE sink — the trajectory event handoff.

A trace is anything with ``.emit(event) -> None``. The runner emits one event
per meaningful step. ``ListTrace`` is the fake (collect into a list); the real
trace storage drops in by implementing the same ``emit``.

Event kinds (the trace "vocabulary"):
    run_started   - run begins; payload carries goal + config
    model_action  - the model produced an action
    tool_call     - the runner is about to invoke a tool
    tool_result   - a tool returned a result dict
    run_finished  - run ended; payload carries termination_reason + final_output
    error         - an exception was captured and turned into a termination
"""

from __future__ import annotations

import time


def event(run_id: str, step: int, kind: str, payload: dict) -> dict:
    """Construct a trace event with a wall-clock timestamp."""
    return {
        "run_id": run_id,
        "step": step,
        "kind": kind,
        "ts": time.time(),
        "payload": payload,
    }


class ListTrace:
    """Collects every emitted event in a list."""

    def __init__(self):
        self.events: list[dict] = []

    def emit(self, event: dict) -> None:
        self.events.append(event)
