"""Runner behavior tests that aren't tied to one scenario fixture.

Covers the provider-agnostic seam between the runner and real model adapters:
the ``model_response`` event that carries a provider's raw payload into the
trace. Uses a tiny stub adapter so no model or network is involved.
"""

from __future__ import annotations

from pathlib import Path

from conftest import VALID_TASK_PATH, FixtureRun, run_task_fixture
from trace_harness.environment.support_env import SupportEnvironment
from trace_harness.models.base import ActionKind, AgentAction, Message, ToolSpec
from trace_harness.runner.agent_runner import AgentRunner
from trace_harness.runner.config import RunConfig
from trace_harness.tasks.loader import load_docs_for_task, load_task
from trace_harness.tracing.artifact_store import ArtifactStore
from trace_harness.tracing.events import TraceEventType


class _StubAdapter:
    """A one-shot adapter that returns a fixed action (with a raw payload)."""

    name = "stub"

    def __init__(self, action: AgentAction) -> None:
        self._action = action

    def next_action(self, transcript: list[Message], tools: list[ToolSpec]) -> AgentAction:
        return self._action


def test_runner_emits_model_response_when_raw_present(tmp_path: Path) -> None:
    task = load_task(VALID_TASK_PATH)
    docs = load_docs_for_task(task, VALID_TASK_PATH)
    environment = SupportEnvironment.from_task(task, docs=docs)
    adapter = _StubAdapter(
        AgentAction(
            kind=ActionKind.FINAL_ANSWER,
            final_answer="done",
            raw={"provider": "gemini", "echo": 1},
        )
    )
    store = ArtifactStore(tmp_path / "runs")
    config = RunConfig(task_id=task.task_id, provider="gemini", model="gemini-2.0-flash")

    result = AgentRunner(adapter, environment, store).run(task, config)

    trace = store.read_trace(result.run_id)
    responses = [e for e in trace if e.event_type is TraceEventType.MODEL_RESPONSE]
    assert len(responses) == 1
    assert responses[0].payload["raw"] == {"provider": "gemini", "echo": 1}
    assert responses[0].step_id == 1
    response_index = next(
        index
        for index, event in enumerate(trace)
        if event.event_type is TraceEventType.MODEL_RESPONSE
    )
    action_index = next(
        index
        for index, event in enumerate(trace)
        if event.event_type is TraceEventType.MODEL_ACTION
    )
    assert response_index < action_index


def test_fixture_run_emits_no_model_response(tmp_path: Path) -> None:
    # Fixture actions carry raw=None, so the runner emits no model_response.
    run: FixtureRun = run_task_fixture(VALID_TASK_PATH, tmp_path / "runs")
    responses = [e for e in run.trace if e.event_type is TraceEventType.MODEL_RESPONSE]
    assert responses == []
