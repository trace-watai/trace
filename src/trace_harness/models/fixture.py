"""FixtureModelAdapter: a scripted, deterministic stand-in for a real model.

What this is
    The adapter replays a pre-authored sequence of :class:`AgentAction`
    objects (a :class:`FixtureScript`). It ignores the transcript and tool
    list entirely.

Why it exists
    - deterministic offline testing of the whole harness;
    - no API tokens in tests or CI, ever;
    - reproducible traces for verifier, attribution, and dashboard fixtures;
    - fast iteration on schemas before any real model is wired in.

What this is NOT
    It is not an agent. It cannot react to observations. A scripted run that
    "fails" is a *staged* failure: useful for building the harness, useless
    for measuring a real model. Real measurement starts when a live adapter
    (Gemini first) replaces this in the run config.

Assumptions
    - One adapter instance serves one run (it keeps a cursor).
    - The script's tool calls are valid against the task's environment; if
      they are not, the runner's validation step records the mismatch in the
      trace rather than this adapter caring.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from trace_harness.models.base import (
    AgentAction,
    Message,
    ScriptExhaustedError,
    ToolSpec,
)

FIXTURE_SCRIPT_SCHEMA_VERSION = "0.1.0"


class FixtureScript(BaseModel):
    """A pre-authored action sequence for one task.

    JSON examples live in ``fixtures/scripts/`` at the repo root. The
    ``reasoning`` field on each action is what makes failure anatomy visible
    to the heuristic attributor — author it deliberately (see
    ``docs/first_vertical_slice.md``).
    """

    schema_version: str = FIXTURE_SCRIPT_SCHEMA_VERSION
    script_id: str
    task_id: str
    description: str = ""
    actions: list[AgentAction] = Field(min_length=1)


class FixtureModelAdapter:
    """Replays a :class:`FixtureScript` one action per ``next_action`` call."""

    name = "fixture"

    def __init__(self, script: FixtureScript):
        self.script = script
        self._cursor = 0

    @classmethod
    def from_file(cls, path: Path | str) -> FixtureModelAdapter:
        script_path = Path(path)
        if not script_path.is_file():
            raise FileNotFoundError(f"fixture script not found: {script_path}")
        try:
            script = FixtureScript.model_validate(
                json.loads(script_path.read_text(encoding="utf-8"))
            )
        except (json.JSONDecodeError, ValidationError) as exc:
            raise ValueError(f"invalid fixture script {script_path}: {exc}") from exc
        return cls(script)

    def next_action(self, transcript: list[Message], tools: list[ToolSpec]) -> AgentAction:
        """Return the next scripted action; transcript and tools are ignored.

        Raises :class:`ScriptExhaustedError` when the script runs out before
        the run terminates — the runner records this as a distinct
        termination reason so an under-length script is never mistaken for a
        model decision.
        """
        if self._cursor >= len(self.script.actions):
            raise ScriptExhaustedError(
                f"fixture script '{self.script.script_id}' exhausted after "
                f"{len(self.script.actions)} actions; the run did not reach a "
                "final answer. Add actions to the script or lower max_steps."
            )
        action = self.script.actions[self._cursor]
        self._cursor += 1
        return action
