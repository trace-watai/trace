"""The TargetAgent bridge: an outside agent's loop driven through the ordinary runner.

Every agent here is plain Python, so these tests need neither LangGraph nor the
OpenAI Agents SDK. ``ScriptAgent`` plays a task's fixture script through the
two callbacks the way an outside agent would, which lets every guarantee be
checked against the fixture provider running the same script.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from conftest import FAILURE_TASK_PATH, FIXTURES_DIR, REPO_ROOT, VALID_TASK_PATH
from trace_harness.attribution.heuristic import HeuristicAttributor
from trace_harness.cli import main
from trace_harness.environment.controls import REFUND_WINDOW_CONTROL_ID, reference_controls
from trace_harness.environment.support_env import SupportEnvironment
from trace_harness.environment.tools import ToolResult
from trace_harness.models.base import Message, MessageRole, ScriptExhaustedError
from trace_harness.models.fixture import FixtureScript
from trace_harness.models.policy import LiveCaller, default_call_policy
from trace_harness.runner import pipeline
from trace_harness.runner.batch import BUDGET_UNENFORCEABLE, BatchRunner, BudgetGuard
from trace_harness.runner.config import RUN_CONFIG_SCHEMA_VERSION, RunConfig
from trace_harness.runner.pipeline import run_task_pipeline
from trace_harness.runner.result import RunStatus, TerminationReason
from trace_harness.runner.suite import (
    SUITE_SCHEMA_VERSION,
    AgentConfig,
    SuiteLoadError,
    SuiteSpec,
    load_suite,
)
from trace_harness.runner.target_agent import (
    RunEnded,
    TargetAgent,
    TargetAgentBridge,
    TargetAgentError,
    TaskPrompt,
    ToolObservation,
    load_target_agent,
    run_target_agent,
)
from trace_harness.tasks.loader import load_docs_for_task, load_task
from trace_harness.tracing import artifact_store as names
from trace_harness.tracing.artifact_store import ArtifactStore
from trace_harness.tracing.events import TraceEvent, TraceEventType
from trace_harness.verifiers.base import VerifierInput, VerifierResult
from trace_harness.verifiers.registry import get_verifier

SCRIPTS_DIR = FIXTURES_DIR / "scripts"
TASK_PATHS = sorted(
    path for path in (FIXTURES_DIR / "tasks").rglob("*.json") if "counterexamples" not in path.parts
)


class ScriptAgent:
    """Plays a task's fixture script as an outside agent would, through callbacks."""

    name = "script-agent"

    def __init__(self, *, forward: bool = True) -> None:
        self.forward = forward
        self.observations: list[ToolObservation] = []
        self.prompt: TaskPrompt | None = None

    def run(self, prompt, tools, call_tool, on_model_response=None) -> str:
        self.prompt = prompt
        script = FixtureScript.model_validate_json(
            (SCRIPTS_DIR / f"{prompt.task_id}_script.json").read_text(encoding="utf-8")
        )
        for turn, action in enumerate(script.actions, start=1):
            if self.forward and on_model_response is not None:
                on_model_response({"turn": turn}, reasoning=action.reasoning)
            if action.final_answer is not None:
                return action.final_answer
            assert action.tool_call is not None
            self.observations.append(
                call_tool(action.tool_call.tool_name, dict(action.tool_call.arguments))
            )
        raise AssertionError("script ended without a final answer")


class UnwiredScriptAgent(ScriptAgent):
    def __init__(self) -> None:
        super().__init__(forward=False)


def _external(ref: str = f"{__name__}:ScriptAgent", **kwargs: Any) -> AgentConfig:
    return AgentConfig(label="external", provider="external", agent_ref=ref, **kwargs)


def _run(
    agent: TargetAgent,
    task_path: Path,
    tmp_path: Path,
    *,
    controls=(),
    max_steps: int = 16,
    timeout_seconds: float = 120.0,
    final_answer_hook=None,
):
    task = load_task(task_path)
    environment = SupportEnvironment.from_task(task, docs=load_docs_for_task(task, task_path))
    for control in controls:
        environment.install_control(control)
    if final_answer_hook is not None:
        environment.register_final_answer_hook(final_answer_hook)
    store = ArtifactStore(tmp_path / "runs")
    config = RunConfig(
        task_id=task.task_id,
        provider="external",
        model=agent.name,
        max_steps=max_steps,
        timeout_seconds=timeout_seconds,
    )
    result = run_target_agent(agent, environment, store, task, config)
    return result, store.read_trace(result.run_id), store


def _events(trace: list[TraceEvent], event_type: TraceEventType) -> list[TraceEvent]:
    return [e for e in trace if e.event_type is event_type]


def _comparable(trace: list[TraceEvent]) -> list[tuple[str, int | None, dict[str, Any]]]:
    """Event type, step and payload, minus what differs by provider on purpose."""
    skip = {TraceEventType.RUN_STARTED, TraceEventType.MODEL_RESPONSE}
    return [(e.event_type.value, e.step_id, e.payload) for e in trace if e.event_type not in skip]


# --- the bridge reproduces the fixture provider exactly ---


@pytest.mark.parametrize("task_path", TASK_PATHS, ids=lambda p: p.stem)
def test_every_task_runs_through_the_bridge_as_the_fixture_provider_does(task_path, tmp_path):
    fixture = run_task_pipeline(
        task_path, AgentConfig(label="fixture"), ArtifactStore(tmp_path / "f"), bundle_on_fail=False
    )
    store = ArtifactStore(tmp_path / "e")
    external = run_task_pipeline(task_path, _external(), store, bundle_on_fail=False)

    assert external.run_result.status == fixture.run_result.status
    assert external.run_result.termination_reason == fixture.run_result.termination_reason
    assert external.run_result.steps_taken == fixture.run_result.steps_taken
    assert external.run_result.final_output == fixture.run_result.final_output
    assert external.verifier_result.verdict == fixture.verifier_result.verdict
    assert [(c.check_id, c.step_ids) for c in external.verifier_result.failed_checks] == [
        (c.check_id, c.step_ids) for c in fixture.verifier_result.failed_checks
    ]
    fixture_trace = ArtifactStore(tmp_path / "f").read_trace(fixture.run_result.run_id)
    external_trace = store.read_trace(external.run_result.run_id)
    assert _comparable(external_trace) == _comparable(fixture_trace)
    # One forwarded response per move, recorded at that move's step.
    responses = _events(external_trace, TraceEventType.MODEL_RESPONSE)
    actions = _events(external_trace, TraceEventType.MODEL_ACTION)
    assert [e.step_id for e in responses] == [e.step_id for e in actions]


def test_the_agent_gets_the_runners_prompt_and_tools(tmp_path):
    agent = ScriptAgent()
    result, trace, _ = _run(agent, VALID_TASK_PATH, tmp_path, max_steps=9)
    first_prompt = _events(trace, TraceEventType.MODEL_PROMPT)[0].payload["new_messages"]
    assert agent.prompt is not None
    assert (agent.prompt.system, agent.prompt.user) == (
        first_prompt[0]["content"],
        first_prompt[1]["content"],
    )
    assert agent.prompt.task_id == "refund_policy_valid_cash"
    assert agent.prompt.max_steps == 9
    assert result.status is RunStatus.COMPLETED


# --- model callback wired and unwired ---


def test_wired_failure_produces_a_full_bundle_with_the_root_cause(tmp_path):
    store = ArtifactStore(tmp_path / "runs")
    result = run_task_pipeline(FAILURE_TASK_PATH, _external(), store)
    run_id = result.run_result.run_id

    attribution = store.read_json(run_id, names.ATTRIBUTION_RESULT)
    assert attribution["root_cause_step"] == 3
    assert attribution["missed_recovery_step"] == 4
    assert attribution["first_irreversible_action_step"] == 5
    for artifact in (names.FAILURE_CARD, names.REPAIR_PACKAGE, names.REGRESSION_ARTIFACT):
        assert store.artifact_path(run_id, artifact).is_file()
    # The regression pins the outside agent's moves, so it replays without it.
    pinned = store.read_json(run_id, names.REGRESSION_ARTIFACT)["pinned_agent_actions"]
    script = json.loads((SCRIPTS_DIR / "refund_policy_failure_script.json").read_text())
    assert pinned == [
        {"provider_state": None, **{"tool_call": None, "final_answer": None, **a}}
        for a in script["actions"]
    ]
    regression = store.read_json(run_id, names.REGRESSION_ARTIFACT)
    assert regression["replay_command"].endswith(f"--agent {__name__}:ScriptAgent")
    config = store.read_json(run_id, names.RUN_CONFIG)
    assert (config["provider"], config["agent_ref"]) == ("external", f"{__name__}:ScriptAgent")
    assert config["model"] == "script-agent"


def test_unwired_agent_still_records_every_move_but_no_reasoning(tmp_path):
    result, trace, store = _run(UnwiredScriptAgent(), FAILURE_TASK_PATH, tmp_path)

    assert _events(trace, TraceEventType.MODEL_RESPONSE) == []
    actions = _events(trace, TraceEventType.MODEL_ACTION)
    assert [e.step_id for e in actions] == list(range(1, 8))
    assert all(e.payload["reasoning"] is None for e in actions)

    task = load_task(FAILURE_TASK_PATH)
    verifier_result = get_verifier("refund_policy").verify(
        VerifierInput.from_parts(
            task=task,
            trace=trace,
            final_state=store.read_json(result.run_id, names.FINAL_STATE),
            run_id=result.run_id,
        )
    )
    attribution = HeuristicAttributor().attribute(task, trace, verifier_result)
    # Reasoning-dependent fields degrade to null; the table in
    # docs/bring_your_own_agent.md quotes these values.
    assert attribution.root_cause_step is None
    assert attribution.first_bad_step == 5
    assert attribution.missed_recovery_step == 4
    assert attribution.first_irreversible_action_step == 5
    assert any("no model reasoning" in note for note in attribution.ambiguity_notes)


def test_responses_attach_to_the_next_move_and_are_made_json_safe(tmp_path):
    class TwoResponses:
        name = "two-responses"

        def run(self, prompt, tools, call_tool, on_model_response=None):
            on_model_response({"id": 1}, reasoning="first thought")
            on_model_response({"id": 2, "when": object()})
            call_tool("get_order", {"customer_name": "Riley Chen"})
            return "done"

    _, trace, _ = _run(TwoResponses(), VALID_TASK_PATH, tmp_path)
    responses = _events(trace, TraceEventType.MODEL_RESPONSE)
    assert [e.step_id for e in responses] == [1]
    raw = responses[0].payload["raw"]["responses"]
    assert raw[0] == {"id": 1}
    assert raw[1]["id"] == 2 and isinstance(raw[1]["when"], str)
    actions = _events(trace, TraceEventType.MODEL_ACTION)
    assert actions[0].payload["reasoning"] == "first thought"
    assert actions[1].payload["reasoning"] is None


def test_a_response_that_is_not_a_dict_is_stored_wrapped(tmp_path):
    """docs/bring_your_own_agent.md says a non-dict raw is stored as {"response": raw}."""

    class TextResponse:
        name = "text-response"

        def run(self, prompt, tools, call_tool, on_model_response=None):
            on_model_response("plain text from the model")
            return "done"

    _, trace, _ = _run(TextResponse(), VALID_TASK_PATH, tmp_path)
    (response,) = _events(trace, TraceEventType.MODEL_RESPONSE)
    assert response.payload["raw"] == {"response": "plain text from the model"}


# --- controls and the final-answer seam ---


def test_installed_control_blocks_the_agents_refund_and_the_agent_sees_it(tmp_path):
    agent = ScriptAgent()
    result, trace, store = _run(agent, FAILURE_TASK_PATH, tmp_path, controls=reference_controls())

    executed = [e for e in _events(trace, TraceEventType.TOOL_CALL_EXECUTED) if e.step_id == 5]
    observed = [e for e in _events(trace, TraceEventType.TOOL_OBSERVATION) if e.step_id == 5]
    assert executed[0].payload["tool_name"] == "issue_refund"
    assert executed[0].payload["blocked_by"] == REFUND_WINDOW_CONTROL_ID
    assert observed[0].payload["blocked_by"] == REFUND_WINDOW_CONTROL_ID
    refund_observation = agent.observations[4]
    assert refund_observation.status == "error"
    assert refund_observation.error == observed[0].payload["error"]
    assert store.read_json(result.run_id, names.FINAL_STATE)["refunds"] == []


def test_final_answer_seam_blocks_the_agents_answer(tmp_path):
    def block(answer, state):
        return ToolResult(
            tool_name="final_answer", status="error", error="ungrounded", blocked_by="ctl_answer"
        )

    result, trace, _ = _run(ScriptAgent(), FAILURE_TASK_PATH, tmp_path, final_answer_hook=block)
    assert result.status is RunStatus.TERMINATED
    assert result.termination_reason is TerminationReason.FINAL_ANSWER_BLOCKED
    assert result.final_output is None
    assert _events(trace, TraceEventType.FINAL_ANSWER)[0].payload["blocked_by"] == "ctl_answer"


# --- limits, failures, and odd agents ---


class _Looping:
    name = "looping"

    def __init__(self) -> None:
        self.ended = threading.Event()
        self.calls = 0

    def run(self, prompt, tools, call_tool, on_model_response=None):
        try:
            while True:
                call_tool("get_order", {"customer_name": "Riley Chen"})
                self.calls += 1
        except RunEnded:
            self.ended.set()
            raise


def test_step_limit_ends_the_run_and_releases_the_agent(tmp_path):
    agent = _Looping()
    result, trace, _ = _run(agent, VALID_TASK_PATH, tmp_path, max_steps=3)

    assert result.termination_reason is TerminationReason.MAX_STEPS_REACHED
    assert result.steps_taken == 3
    assert len(_events(trace, TraceEventType.TOOL_CALL_EXECUTED)) == 3
    assert agent.ended.wait(5)
    # The third call's result never reached the agent; the run was already over.
    assert agent.calls == 2


def test_a_hung_agent_times_out_and_nothing_runs_after(tmp_path):
    release = threading.Event()
    finished = threading.Event()
    outcome: list[BaseException] = []

    class Hung:
        name = "hung"

        def run(self, prompt, tools, call_tool, on_model_response=None):
            release.wait(10)
            try:
                call_tool("issue_refund", {"customer_name": "Riley Chen", "refund_type": "cash"})
            except RunEnded as exc:
                outcome.append(exc)
                raise
            finally:
                finished.set()
            return "unreachable"

    result, _, store = _run(Hung(), VALID_TASK_PATH, tmp_path, timeout_seconds=0.3)
    assert result.termination_reason is TerminationReason.TIMEOUT
    release.set()
    assert finished.wait(5)
    assert len(outcome) == 1
    assert _events(store.read_trace(result.run_id), TraceEventType.TOOL_CALL_REQUESTED) == []
    assert store.read_json(result.run_id, names.FINAL_STATE)["refunds"] == []


def test_agent_exception_is_recorded_as_a_model_error(tmp_path):
    class Crashes:
        name = "crashes"

        def run(self, prompt, tools, call_tool, on_model_response=None):
            call_tool("get_order", {"customer_name": "Riley Chen"})
            raise ValueError("boom")

    result, trace, _ = _run(Crashes(), VALID_TASK_PATH, tmp_path)
    assert result.status is RunStatus.ERROR
    assert result.termination_reason is TerminationReason.MODEL_ERROR
    errors = _events(trace, TraceEventType.ERROR)
    assert errors[0].step_id == 2
    assert errors[0].payload["kind"] == "model_error"
    assert "ValueError: boom" in errors[0].payload["error"]


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (ValueError("could not parse the model's reply"), "model_error"),
        (ScriptExhaustedError("script 'x' exhausted after 1 actions"), "script_exhausted"),
    ],
    ids=["raised", "script_exhausted"],
)
def test_responses_forwarded_before_the_agent_raises_are_recorded(error, kind, tmp_path):
    class ForwardsThenRaises:
        name = "forwards-then-raises"

        def run(self, prompt, tools, call_tool, on_model_response=None):
            call_tool("get_order", {"customer_name": "Riley Chen"})
            on_model_response({"id": "resp_2", "output": "unparseable"}, reasoning="thinking")
            raise error

    result, trace, _ = _run(ForwardsThenRaises(), VALID_TASK_PATH, tmp_path)
    assert result.termination_reason.value == kind
    at_step_2 = [(e.event_type, e.payload) for e in trace if e.step_id == 2]
    kinds = [event_type for event_type, _ in at_step_2]
    # The response is recorded at the failing step, ahead of the error.
    assert kinds[-2:] == [TraceEventType.MODEL_RESPONSE, TraceEventType.ERROR]
    assert at_step_2[-2][1]["raw"] == {"id": "resp_2", "output": "unparseable"}
    assert at_step_2[-1][1]["kind"] == kind
    assert _events(trace, TraceEventType.MODEL_ACTION)[-1].step_id == 1


def test_a_scripted_model_running_out_keeps_its_termination_reason(tmp_path):
    class RunsOut:
        name = "runs-out"

        def run(self, prompt, tools, call_tool, on_model_response=None):
            raise ScriptExhaustedError("script 'x' exhausted after 0 actions")

    result, _, _ = _run(RunsOut(), VALID_TASK_PATH, tmp_path)
    assert result.termination_reason is TerminationReason.SCRIPT_EXHAUSTED
    assert "runs-out" in (result.error or "")


def test_a_non_string_answer_is_a_model_error(tmp_path):
    class Dict:
        name = "dict"

        def run(self, prompt, tools, call_tool, on_model_response=None):
            return {"answer": "done"}

    result, _, _ = _run(Dict(), VALID_TASK_PATH, tmp_path)
    assert result.termination_reason is TerminationReason.MODEL_ERROR
    assert "instead of a final answer string" in (result.error or "")


def test_invalid_calls_are_validated_and_never_executed(tmp_path):
    seen: list[ToolObservation] = []

    class Sloppy:
        name = "sloppy"

        def run(self, prompt, tools, call_tool, on_model_response=None):
            seen.append(call_tool("wire_money", {}))
            seen.append(call_tool("issue_refund", {"customer_name": "Riley Chen", "amt": 5}))
            return "gave up"

    result, trace, store = _run(Sloppy(), VALID_TASK_PATH, tmp_path)
    validated = _events(trace, TraceEventType.TOOL_CALL_VALIDATED)
    assert [e.payload["valid"] for e in validated] == [False, False]
    assert _events(trace, TraceEventType.TOOL_CALL_EXECUTED) == []
    assert [o.status for o in seen] == ["error", "error"]
    assert "unknown tool" in (seen[0].error or "")
    assert result.status is RunStatus.COMPLETED
    assert store.read_json(result.run_id, names.FINAL_STATE)["refunds"] == []


def test_parallel_tool_calls_become_consecutive_steps(tmp_path):
    names_ = ["Riley Chen", "Nobody A", "Nobody B", "Nobody C", "Nobody D", "Nobody E"]
    got: dict[str, ToolObservation] = {}

    class Parallel:
        name = "parallel"

        def run(self, prompt, tools, call_tool, on_model_response=None):
            def look_up(name: str) -> None:
                got[name] = call_tool("get_order", {"customer_name": name})

            threads = [threading.Thread(target=look_up, args=(name,)) for name in names_]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
            return "looked them up"

    result, trace, _ = _run(Parallel(), VALID_TASK_PATH, tmp_path)
    assert result.steps_taken == len(names_) + 1
    executed = _events(trace, TraceEventType.TOOL_CALL_EXECUTED)
    assert [e.step_id for e in executed] == list(range(1, len(names_) + 1))
    # Each caller got the result of its own call, whatever order they ran in.
    assert got["Riley Chen"].status == "ok"
    for name in names_[1:]:
        assert name in (got[name].error or "")


def test_parallel_calls_left_over_at_the_step_limit_are_all_released(tmp_path):
    outcomes: list[str] = []
    done = threading.Event()

    class Flood:
        name = "flood"

        def run(self, prompt, tools, call_tool, on_model_response=None):
            def look_up() -> None:
                try:
                    call_tool("get_order", {"customer_name": "Riley Chen"})
                    outcomes.append("result")
                except RunEnded:
                    outcomes.append("ended")

            threads = [threading.Thread(target=look_up) for _ in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
            done.set()
            return "unreachable"

    result, trace, _ = _run(Flood(), VALID_TASK_PATH, tmp_path, max_steps=2)
    assert result.termination_reason is TerminationReason.MAX_STEPS_REACHED
    assert len(_events(trace, TraceEventType.TOOL_CALL_EXECUTED)) == 2
    # One call got its result back. The one in flight and the four queued
    # behind it are all released when the run ends.
    assert done.wait(5)
    assert sorted(outcomes) == ["ended"] * 5 + ["result"]


def _prompt_messages() -> list[Message]:
    return [
        Message(role=MessageRole.SYSTEM, content="system"),
        Message(role=MessageRole.USER, content="user"),
    ]


def test_a_call_taken_by_an_abandoned_move_is_released_when_the_bridge_closes():
    """The runner abandons next_action on a timeout, and close() can run while it is mid-move.

    Here the abandoned move has already taken the agent's tool call off the
    queue when close() drains it. The call must still end with RunEnded
    instead of waiting forever for a result nobody will send.
    """
    taken = threading.Event()
    closed = threading.Event()
    outcome: list[str] = []

    class OneCall:
        name = "one-call"

        def run(self, prompt, tools, call_tool, on_model_response=None):
            try:
                call_tool("get_order", {"customer_name": "Riley Chen"})
                outcome.append("result")
            except RunEnded:
                outcome.append("ended")
                raise
            return "done"

    bridge = TargetAgentBridge(OneCall(), task_id="t", max_steps=4)
    drain = bridge._drain_responses

    def paused_after_taking_the_call():
        taken.set()
        closed.wait(5)
        return drain()

    bridge._drain_responses = paused_after_taking_the_call
    errors: list[BaseException] = []

    def abandoned_move() -> None:
        try:
            bridge.next_action(_prompt_messages(), [])
        except TargetAgentError as exc:
            errors.append(exc)

    mover = threading.Thread(target=abandoned_move, daemon=True)
    mover.start()
    assert taken.wait(5)
    bridge.close()
    closed.set()
    mover.join(5)
    assert bridge._thread is not None
    bridge._thread.join(5)
    assert not bridge._thread.is_alive()
    assert outcome == ["ended"]
    assert [str(e) for e in errors] == ["the target agent bridge is closed"]


def test_close_and_a_late_move_never_both_answer_the_same_call():
    """close() and an abandoned next_action can both reach the call waiting on a result.

    Whichever answers first wins and the other is ignored, so close() never
    raises from a full reply queue however the two interleave.
    """
    release = threading.Event()

    class TwoCalls:
        name = "two-calls"

        def run(self, prompt, tools, call_tool, on_model_response=None):
            call_tool("get_order", {"customer_name": "Riley Chen"})
            release.wait(5)
            return "done"

    bridge = TargetAgentBridge(TwoCalls(), task_id="t", max_steps=4)
    first = bridge.next_action(_prompt_messages(), [])
    assert first.tool_call is not None
    observation = Message(
        role=MessageRole.TOOL,
        content="",
        metadata={"tool_name": "get_order", "status": "ok", "result": {}, "error": None},
    )
    pending = bridge._pending
    assert pending is not None
    # The late move answers the waiting call just before close() reaches it.
    pending.reply.put_nowait(ToolObservation(tool_name="get_order", status="ok"))
    bridge.close()
    release.set()
    with pytest.raises(TargetAgentError, match="closed"):
        bridge.next_action([*_prompt_messages(), observation], [])


def test_a_transcript_without_the_observation_still_leaves_the_call_for_close():
    outcome: list[str] = []

    class OneCall:
        name = "one-call"

        def run(self, prompt, tools, call_tool, on_model_response=None):
            try:
                call_tool("get_order", {"customer_name": "Riley Chen"})
            except RunEnded:
                outcome.append("ended")
                raise
            return "done"

    bridge = TargetAgentBridge(OneCall(), task_id="t", max_steps=4)
    bridge.next_action(_prompt_messages(), [])
    with pytest.raises(TargetAgentError, match="expected the runner's tool observation"):
        bridge.next_action(_prompt_messages(), [])
    bridge.close()
    assert bridge._thread is not None
    bridge._thread.join(5)
    assert outcome == ["ended"]


# --- loading agents and configuring runs ---

AGENT_INSTANCE = ScriptAgent()


def make_agent() -> ScriptAgent:
    return ScriptAgent()


@pytest.mark.parametrize("attribute", ["ScriptAgent", "AGENT_INSTANCE", "make_agent"])
def test_agent_refs_resolve_classes_instances_and_factories(attribute):
    agent = load_target_agent(f"{__name__}:{attribute}")
    assert isinstance(agent, ScriptAgent)


@pytest.mark.parametrize(
    ("ref", "message"),
    [
        ("no_colon_here", "must look like"),
        ("trace_harness.no_such_module:agent", "cannot import"),
        (f"{__name__}:missing", "has no"),
        (f"{__name__}:SCRIPTS_DIR", "neither a target agent nor a factory"),
        (f"{__name__}:_external", "did not produce a target agent"),
        (f"{__name__}:_run", "must take no arguments"),
    ],
)
def test_bad_agent_refs_are_input_errors(ref, message):
    with pytest.raises(ValueError, match=message):
        load_target_agent(ref)


def test_run_pipeline_with_agent_flag_writes_an_external_run(tmp_path, capsys):
    runs = tmp_path / "runs"
    code = main(
        [
            "run-pipeline",
            str(FAILURE_TASK_PATH),
            "--agent",
            f"{__name__}:ScriptAgent",
            "--runs-dir",
            str(runs),
        ]
    )
    assert code == 0
    (run_dir,) = [p for p in runs.iterdir() if p.is_dir()]
    config = json.loads((run_dir / names.RUN_CONFIG).read_text())
    assert config["schema_version"] == "0.4.0"
    assert (config["provider"], config["model"]) == ("external", "script-agent")
    assert config["agent_ref"] == f"{__name__}:ScriptAgent"
    # The harness makes no model call for an outside agent, so no call policy applied.
    assert config["call_policy"] is None
    assert json.loads((run_dir / names.ATTRIBUTION_RESULT).read_text())["root_cause_step"] == 3
    index = json.loads((runs / "index.json").read_text())["entries"][0]
    assert (index["provider"], index["model"]) == ("external", "script-agent")


@pytest.mark.parametrize(
    "extra",
    [
        ["--provider", "external"],
        ["--agent", f"{__name__}:ScriptAgent", "--provider", "gemini"],
        ["--agent", f"{__name__}:ScriptAgent", "--seed", "3"],
        ["--agent", f"{__name__}:ScriptAgent", "--cassette-mode", "replay"],
        ["--agent", f"{__name__}:ScriptAgent", "--script", "x.json"],
        ["--agent", "not_a_ref"],
    ],
)
def test_agent_flag_misuse_is_an_input_error(extra, tmp_path, capsys):
    argv = ["run-fixture", str(VALID_TASK_PATH), "--runs-dir", str(tmp_path), *extra]
    assert main(argv) == 2
    assert "error:" in capsys.readouterr().err


def test_agent_config_requires_agent_ref_exactly_for_external():
    with pytest.raises(ValueError, match="needs agent_ref"):
        AgentConfig(label="x", provider="external")
    with pytest.raises(ValueError, match="only valid with provider 'external'"):
        AgentConfig(label="x", agent_ref=f"{__name__}:ScriptAgent")
    with pytest.raises(ValueError, match="cannot use a harness cassette"):
        _external(cassette={"mode": "replay"})
    assert _external().agent_ref == f"{__name__}:ScriptAgent"


@pytest.mark.parametrize(
    "setting", [{"temperature": 0.0}, {"seed": 7}], ids=lambda s: next(iter(s))
)
def test_a_suite_refuses_model_settings_for_an_outside_agent(setting, tmp_path):
    """The CLI refuses --temperature and --seed with --agent, and a suite does the same.

    Accepting them would write a run_config.json claiming settings the harness
    never applied, since the outside agent owns its model.
    """
    with pytest.raises(ValueError, match="the outside agent owns its model"):
        _external(**setting)
    manifest = tmp_path / "suite.json"
    manifest.write_text(
        json.dumps(
            {
                "suite_id": "byoa",
                "tasks": [str(VALID_TASK_PATH)],
                "agent_configs": [
                    {"label": "x", "provider": "external", "agent_ref": "m:f", **setting}
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(SuiteLoadError, match="the outside agent owns its model"):
        load_suite(manifest)
    # A null setting is what every other external config carries, and still loads.
    assert _external(temperature=None, seed=None).temperature is None


def test_existing_suites_and_run_configs_still_load():
    for suite in sorted((FIXTURES_DIR / "suites").glob("*.json")):
        assert all(c.agent_ref is None for c in load_suite(suite).agent_configs)
    configs = [
        RunConfig.model_validate_json(path.read_text())
        for path in sorted(REPO_ROOT.glob("docs/acceptance/**/" + names.RUN_CONFIG))
    ]
    # RunConfig 0.4.0 added agent_ref; #196 took 0.3.0 for call_policy.
    older = [config for config in configs if config.schema_version < "0.4.0"]
    assert older
    assert all(config.agent_ref is None for config in older)
    # The retained reference-agent runs were produced at 0.4.0 and say which agent ran.
    external = [config for config in configs if config.provider == "external"]
    assert len(external) == 2
    for config in external:
        assert config.schema_version == "0.4.0"
        assert config.agent_ref and config.agent_ref.startswith("trace_harness.agents.")
        assert config.call_policy is None


def test_suite_runs_an_external_agent_config(tmp_path):
    store = ArtifactStore(tmp_path / "runs")
    result = run_task_pipeline(VALID_TASK_PATH, _external(model="labelled"), store)
    assert result.run_config.model == "labelled"
    assert result.verifier_result is not None
    assert VerifierResult.model_validate(result.verifier_result).passed


# --- the #196 call policy and budget guard ---


def test_an_external_run_makes_no_provider_call_and_records_no_call_policy(tmp_path, monkeypatch):
    """The bridge is not a live adapter, so no call policy wraps it (#196)."""

    def forbidden(*args, **kwargs):
        raise AssertionError("an outside agent's run must not reach the live call path")

    monkeypatch.setattr(pipeline, "create_model_adapter", forbidden)
    monkeypatch.setattr(LiveCaller, "call", forbidden)
    store = ArtifactStore(tmp_path / "runs")
    result = run_task_pipeline(VALID_TASK_PATH, _external(), store)

    assert result.run_result.status is RunStatus.COMPLETED
    assert result.run_config.call_policy is None
    config = store.read_json(result.run_result.run_id, names.RUN_CONFIG)
    assert config["schema_version"] == "0.4.0"
    assert config["call_policy"] is None
    # A suite cannot hand the harness a policy for calls it never makes.
    with pytest.raises(ValueError, match="cannot use a call_policy"):
        _external(call_policy=default_call_policy("openai"))


def test_an_agent_error_ends_the_run_at_once_and_is_never_retried(tmp_path, monkeypatch):
    """A bridge error is a model error on its first occurrence, whatever it looks like."""
    asked: list[int] = []
    next_action = TargetAgentBridge.next_action

    def counted(self, transcript, tools):
        asked.append(len(transcript))
        return next_action(self, transcript, tools)

    monkeypatch.setattr(TargetAgentBridge, "next_action", counted)

    class DropsTheConnection:
        name = "drops-the-connection"

        def run(self, prompt, tools, call_tool, on_model_response=None):
            call_tool("get_order", {"customer_name": "Riley Chen"})
            # The live call policy would retry this from a provider SDK.
            raise ConnectionResetError("connection reset by peer")

    started = time.monotonic()
    result, trace, _ = _run(DropsTheConnection(), VALID_TASK_PATH, tmp_path, timeout_seconds=30)
    elapsed = time.monotonic() - started

    assert result.termination_reason is TerminationReason.MODEL_ERROR
    # One tool call and the failed move. Nothing asked the ended agent again.
    assert len(asked) == 2
    assert elapsed < 5
    (error,) = _events(trace, TraceEventType.ERROR)
    assert error.payload["kind"] == "model_error"
    assert "ConnectionResetError" in error.payload["error"]
    assert "call_record" not in error.payload


class _Answers:
    name = "answers"

    def run(self, prompt, tools, call_tool, on_model_response=None):
        return "done"


class _Raises:
    name = "raises"

    def run(self, prompt, tools, call_tool, on_model_response=None):
        raise TimeoutError("the agent's own model call timed out")


@pytest.mark.parametrize("agent", [_Answers(), _Raises()], ids=["answered", "raised"])
def test_asking_an_ended_agent_for_another_move_fails_at_once(agent):
    """A retry of the last move would otherwise wait out the whole time budget."""
    bridge = TargetAgentBridge(agent, task_id="t", max_steps=4)
    transcript = [
        Message(role=MessageRole.SYSTEM, content="system"),
        Message(role=MessageRole.USER, content="user"),
    ]
    try:
        bridge.next_action(transcript, [])
    except TargetAgentError:
        pass
    assert bridge._thread is not None
    bridge._thread.join(5)

    outcome: list[BaseException] = []

    def ask_again() -> None:
        try:
            bridge.next_action(transcript, [])
        except BaseException as exc:  # noqa: BLE001 - inspected below
            outcome.append(exc)

    again = threading.Thread(target=ask_again, daemon=True)
    again.start()
    again.join(2)
    waiting = again.is_alive()
    bridge.close()
    assert not waiting
    assert isinstance(outcome[0], TargetAgentError)
    assert "has ended and has no further move" in str(outcome[0])


def test_a_capped_suite_refuses_an_outside_agent_as_unenforceable(tmp_path):
    """The harness cannot see what an outside agent's model calls cost (#196)."""
    guard = BudgetGuard(10.0)
    assert not guard.admit("external", "script-agent")
    assert guard.stop_reason == BUDGET_UNENFORCEABLE
    assert BudgetGuard(None).admit("external", "script-agent")

    suite = SuiteSpec(
        suite_id="byoa_capped",
        tasks=[str(VALID_TASK_PATH)],
        agent_configs=[_external()],
        max_cost_usd=10.0,
    )
    summary = BatchRunner(ArtifactStore(tmp_path / "runs")).run(suite)
    assert summary.entries == []
    assert summary.budget is not None
    assert summary.budget.stop_reason == BUDGET_UNENFORCEABLE
    assert "outside agent" in (summary.budget.detail or "")
    assert [cell.agent_label for cell in summary.budget.not_run] == ["external"]

    suite_path = tmp_path / "suite.json"
    suite_path.write_text(suite.model_dump_json(), encoding="utf-8")
    runs = tmp_path / "cli_runs"
    assert main(["--runs-dir", str(runs), "run-suite", str(suite_path)]) == 2
    assert not [p for p in runs.glob("run_*") if p.is_dir()]


def test_an_uncapped_suite_runs_an_outside_agent_with_an_unknown_cost(tmp_path):
    suite = SuiteSpec(
        suite_id="byoa_uncapped", tasks=[str(VALID_TASK_PATH)], agent_configs=[_external()]
    )
    summary = BatchRunner(ArtifactStore(tmp_path / "runs")).run(suite)
    assert summary.budget is None
    (entry,) = summary.entries
    assert (entry.provider, entry.verdict) == ("external", "pass")
    # Never reported as free, since the harness did not see the spend.
    assert entry.cost_usd is None


def test_both_schema_bumps_sit_above_the_196_versions():
    """#196 took RunConfig and Suite 0.3.0 for call_policy and max_cost_usd."""
    assert RUN_CONFIG_SCHEMA_VERSION == "0.4.0"
    assert SUITE_SCHEMA_VERSION == "0.4.0"
    suite = SuiteSpec(suite_id="byoa", tasks=[str(VALID_TASK_PATH)], agent_configs=[_external()])
    written = json.loads(suite.model_dump_json())
    assert written["schema_version"] == "0.4.0"
    assert written["agent_configs"][0]["agent_ref"] == f"{__name__}:ScriptAgent"
    # Files written at 0.3.0, with a cap and a call policy and no outside agent, still load.
    older_suite = SuiteSpec.model_validate(
        {
            "schema_version": "0.3.0",
            "suite_id": "capped",
            "tasks": [str(VALID_TASK_PATH)],
            "max_cost_usd": 1.0,
            "agent_configs": [
                {"label": "g", "provider": "gemini", "call_policy": {"max_attempts": 2}}
            ],
        }
    )
    assert older_suite.agent_configs[0].agent_ref is None
    older_config = RunConfig.model_validate(
        {
            "schema_version": "0.3.0",
            "task_id": "t",
            "provider": "gemini",
            "call_policy": {"max_attempts": 2},
        }
    )
    assert (older_config.agent_ref, older_config.call_policy.max_attempts) == (None, 2)
