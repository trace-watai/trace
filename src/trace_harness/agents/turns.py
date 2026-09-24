"""Where a reference agent's model turns come from, and how its transcript maps back.

A reference agent talks to its framework's model interface (a LangChain chat
model, an Agents SDK ``Model``). Behind that interface sits an ordinary harness
:class:`~trace_harness.models.base.ModelAdapter`, which is where the turns come
from.

- :class:`ScriptedTurns` plays the task's fixture script (the file its
  ``metadata.fixture_script`` names, as for the fixture provider) and ignores
  what the model is sent.
- :class:`CassetteTurns` replays a harness model cassette, or records one around
  another turn source. Replay checks every request against the recording, so a
  change in what the agent sends its model fails loudly. The same cassette
  format serves a real model once one is recorded, with no change to the agent.

The helpers below turn a framework conversation back into the harness
transcript shape (the runner's own message builders are reused), so a cassette
fingerprints the same kind of transcript for every agent.

Both sources find the committed fixtures from this package's location in the
repository, so a reference agent runs the same from any working directory. They
need the source checkout (an editable install); a wheel carries no fixtures.
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
    action_to_assistant_message,
    observation_to_tool_message,
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

REPO_ROOT = Path(__file__).resolve().parents[3]
CASSETTE_ROOT = REPO_ROOT / "fixtures" / "cassettes"
TASKS_DIR = REPO_ROOT / "fixtures" / "tasks"


def fixture_script_for(task_id: str, tasks_dir: Path = TASKS_DIR) -> Path:
    """The fixture script a task names in ``metadata.fixture_script``, found by task id.

    The task file is looked up under ``tasks_dir`` and the script resolved
    against its directory, as the fixture provider resolves it, so a scripted
    reference agent plays the same file the fixture provider plays for that task.
    """
    matches: list[tuple[Path, dict]] = []
    for path in sorted(Path(tasks_dir).rglob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("task_id") == task_id:
            matches.append((path, data))
    if len(matches) != 1:
        found = "no task file" if not matches else f"{len(matches)} task files"
        raise FileNotFoundError(f"{found} with task_id {task_id!r} under {tasks_dir}")
    path, data = matches[0]
    script = (data.get("metadata") or {}).get("fixture_script")
    if not script:
        raise FileNotFoundError(f"task {task_id!r} ({path}) has no metadata.fixture_script")
    return (path.parent / script).resolve()


@dataclass(frozen=True)
class ScriptedTurns:
    """Turns from a fixture script, whatever the model is sent.

    Each task plays the script its own ``metadata.fixture_script`` names (see
    :func:`fixture_script_for`). ``script`` plays one given file instead, which
    is how :func:`record_cassette` plays the script of the task file it was given.
    """

    tasks_dir: Path = TASKS_DIR
    script: Path | None = None
    label: str = field(default="scripted", init=False)

    def __call__(self, prompt: TaskPrompt) -> ModelAdapter:
        script = self.script
        if script is None:
            script = fixture_script_for(prompt.task_id, self.tasks_dir)
        return FixtureModelAdapter.from_file(script)


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
    return action_to_assistant_message(action)


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
    return observation_to_tool_message(result)


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
    turns = CassetteTurns(namespace, root=root, mode="record", inner=ScriptedTurns(script=script))
    agent = make_agent(turns)
    environment = SupportEnvironment.from_task(task, docs=load_docs_for_task(task, task_path))
    config = RunConfig(task_id=task.task_id, provider=EXTERNAL_PROVIDER, model=agent.name)
    return run_target_agent(agent, environment, ArtifactStore(runs_dir), task, config)
