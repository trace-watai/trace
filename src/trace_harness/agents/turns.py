"""Where a reference agent's model turns come from, and how its transcript maps back.

A reference agent talks to its framework's model interface (a LangChain chat
model, an Agents SDK ``Model``). Behind that interface sits an ordinary harness
:class:`~trace_harness.models.base.ModelAdapter`, which is where the turns come
from.

- :class:`ScriptedTurns` plays the task's fixture script and ignores what the
  model is sent.
- :class:`CassetteTurns` replays a harness model cassette, or records one around
  another turn source. Replay checks every request against the recording, so a
  change in what the agent sends its model fails loudly. The same cassette
  format serves a real model once one is recorded, with no change to the agent.

The helpers below turn a framework conversation back into the harness
transcript shape (the runner's own message builders are reused), so a cassette
fingerprints the same kind of transcript for every agent.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from trace_harness.environment.support_env import SupportEnvironment
from trace_harness.environment.tools import ToolResult
from trace_harness.models.base import AgentAction, Message, MessageRole, ModelAdapter
from trace_harness.models.cassette import (
    CassetteError,
    CassetteRequestConfig,
    RecordingModelAdapter,
    cassette_path,
)
from trace_harness.models.fixture import FixtureModelAdapter
from trace_harness.runner.agent_runner import (
    _action_to_assistant_message,
    _observation_to_tool_message,
)
from trace_harness.runner.config import RunConfig
from trace_harness.runner.result import RunResult
from trace_harness.runner.target_agent import (
    EXTERNAL_PROVIDER,
    TargetAgent,
    TaskPrompt,
    ToolObservation,
    run_target_agent,
)
from trace_harness.tasks.loader import load_docs_for_task, load_task
from trace_harness.tracing.artifact_store import ArtifactStore

TurnSource = Callable[[TaskPrompt], ModelAdapter]

CASSETTE_ROOT = Path("fixtures/cassettes")
SCRIPTS_DIR = Path("fixtures/scripts")


@dataclass(frozen=True)
class ScriptedTurns:
    """Turns from ``<scripts_dir>/<task_id>_script.json``, whatever the model is sent."""

    scripts_dir: Path = SCRIPTS_DIR
    label: str = field(default="scripted", init=False)

    def __call__(self, prompt: TaskPrompt) -> ModelAdapter:
        return FixtureModelAdapter.from_file(self.scripts_dir / f"{prompt.task_id}_script.json")


@dataclass(frozen=True)
class CassetteTurns:
    """Turns replayed from, or recorded into, ``<root>/<namespace>/<task_id>/<model>/``.

    ``namespace`` keeps each reference agent's cassettes apart, because two
    agents send their models different requests for the same task. ``model``
    names what produced the recording; the committed ones say ``scripted``.
    """

    namespace: str
    root: Path = CASSETTE_ROOT
    mode: Literal["record", "replay"] = "replay"
    inner: TurnSource | None = None
    model: str = "scripted"

    @property
    def label(self) -> str:
        return f"cassette:{self.model}"

    def config(self, task_id: str) -> CassetteRequestConfig:
        return CassetteRequestConfig(task_id=task_id, provider=self.namespace, model=self.model)

    def path(self, task_id: str) -> Path:
        return cassette_path(self.root / self.namespace, self.config(task_id))

    def __call__(self, prompt: TaskPrompt) -> ModelAdapter:
        if self.mode == "record" and self.inner is None:
            raise CassetteError("recording a reference cassette needs an inner turn source")
        return RecordingModelAdapter(
            mode=self.mode,
            path=self.path(prompt.task_id),
            config=self.config(prompt.task_id),
            inner=self.inner(prompt) if self.mode == "record" and self.inner else None,
        )


def assistant_message(action: AgentAction) -> Message:
    """An agent's model turn as the harness runner would have written it."""
    return _action_to_assistant_message(action)


def observation_text(observation: ToolObservation) -> str:
    """What a reference agent hands its model as a tool result."""
    return observation.model_dump_json()


def tool_message(tool_name: str, content: str) -> Message:
    """A tool result from the agent's conversation, back in harness form.

    Content the reference tools wrote round-trips exactly; anything else is
    kept as plain text under the tool's name.
    """
    try:
        observation = ToolObservation.model_validate_json(content)
        result = ToolResult(
            tool_name=observation.tool_name,
            status=observation.status,
            result=observation.result or {},
            error=observation.error,
        )
    except (ValidationError, ValueError):
        return Message(role=MessageRole.TOOL, content=content, metadata={"tool_name": tool_name})
    return _observation_to_tool_message(result)


def tool_arguments(raw: str | dict) -> dict:
    """Tool-call arguments as a dict, whether the framework passed text or an object."""
    if isinstance(raw, dict):
        return raw
    parsed = json.loads(raw or "{}")
    if not isinstance(parsed, dict):
        raise ValueError(f"tool arguments must be a JSON object, got {raw!r}")
    return parsed


def record_cassette(
    make_agent: Callable[[TurnSource], TargetAgent],
    task_path: Path | str,
    *,
    namespace: str,
    root: Path,
    runs_dir: Path,
) -> RunResult:
    """Run a reference agent on one task while recording its scripted turns.

    The committed reference cassettes were made this way, and the reference
    agent tests re-record them into a temporary directory to show they still
    come out byte for byte the same.
    """
    task_path = Path(task_path).resolve()
    task = load_task(task_path)
    script = (task_path.parent / task.metadata["fixture_script"]).resolve()
    turns = CassetteTurns(
        namespace, root=root, mode="record", inner=ScriptedTurns(scripts_dir=script.parent)
    )
    agent = make_agent(turns)
    environment = SupportEnvironment.from_task(task, docs=load_docs_for_task(task, task_path))
    config = RunConfig(task_id=task.task_id, provider=EXTERNAL_PROVIDER, model=agent.name)
    return run_target_agent(agent, environment, ArtifactStore(runs_dir), task, config)
