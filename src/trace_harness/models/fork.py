"""ForkAdapter: recorded actions through a step, then another adapter (#159).

The branch stage continues a recorded run from a chosen step with a different
agent. Putting the switch here keeps the runner free of it: the runner asks
one adapter for every step and never learns that the answers change hands.

``switch_at_step`` is the last step the recording answers. Steps 1 through
``switch_at_step`` come from ``prefix``, and every step after it comes from
``continuation``, which sees the full transcript, recorded turns included.
``switch_at_step=0`` hands the whole run to the continuation.
"""

from __future__ import annotations

from trace_harness.models.base import AgentAction, Message, ModelAdapter, ToolSpec
from trace_harness.models.fixture import FixtureModelAdapter


class ForkAdapter:
    """Serve recorded actions, then delegate. Single-run, like its parts."""

    def __init__(
        self, prefix: FixtureModelAdapter, continuation: ModelAdapter, switch_at_step: int
    ):
        if switch_at_step < 0:
            raise ValueError(f"switch_at_step must be 0 or more, got {switch_at_step}")
        self.prefix = prefix
        self.continuation = continuation
        self.switch_at_step = switch_at_step
        self.name = continuation.name
        self._step = 0

    def next_action(self, transcript: list[Message], tools: list[ToolSpec]) -> AgentAction:
        self._step += 1
        if self._step <= self.switch_at_step:
            return self.prefix.next_action(transcript, tools)
        return self.continuation.next_action(transcript, tools)
