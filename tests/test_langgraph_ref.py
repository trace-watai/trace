"""The LangGraph reference agent: a real graph loop over a scripted model, offline.

Skipped when the langgraph extra is not installed.
"""

from __future__ import annotations

import json

import pytest

from conftest import FAILURE_TASK_PATH, REPO_ROOT, VALID_TASK_PATH
from trace_harness.agents.turns import CassetteTurns, ScriptedTurns, record_cassette
from trace_harness.attribution.heuristic import HeuristicAttributor
from trace_harness.cli import main
from trace_harness.environment.controls import REFUND_WINDOW_CONTROL_ID, reference_controls
from trace_harness.environment.support_env import SupportEnvironment
from trace_harness.models.base import MessageRole
from trace_harness.runner.config import RunConfig
from trace_harness.runner.pipeline import run_task_pipeline
from trace_harness.runner.result import RunStatus, TerminationReason
from trace_harness.runner.suite import AgentConfig
from trace_harness.runner.target_agent import run_target_agent
from trace_harness.tasks.loader import load_docs_for_task, load_task
from trace_harness.tracing import artifact_store as names
from trace_harness.tracing.artifact_store import ArtifactStore
from trace_harness.tracing.events import TraceEventType
from trace_harness.verifiers.base import VerifierInput
from trace_harness.verifiers.registry import get_verifier

langgraph_ref = pytest.importorskip(
    "trace_harness.agents.langgraph_ref", reason="needs the langgraph extra", exc_type=ImportError
)

REF = "trace_harness.agents.langgraph_ref:agent"
COMMITTED = REPO_ROOT / "fixtures" / "cassettes"


@pytest.fixture(autouse=True)
def repo_root(monkeypatch):
    # Cassette and script paths are repo-relative, like every CLI fixture path.
    monkeypatch.chdir(REPO_ROOT)


def _run(agent, task_path, tmp_path, *, controls=(), max_steps=16):
    task = load_task(task_path)
    environment = SupportEnvironment.from_task(task, docs=load_docs_for_task(task, task_path))
    for control in controls:
        environment.install_control(control)
    store = ArtifactStore(tmp_path / "runs")
    config = RunConfig(
        task_id=task.task_id, provider="external", model=agent.name, max_steps=max_steps
    )
    result = run_target_agent(agent, environment, store, task, config)
    return result, store.read_trace(result.run_id), store


def _events(trace, event_type):
    return [e for e in trace if e.event_type is event_type]


def test_reference_graph_passes_the_valid_cash_task(tmp_path):
    external = AgentConfig(label="lg", provider="external", agent_ref=REF)
    result = run_task_pipeline(VALID_TASK_PATH, external, ArtifactStore(tmp_path / "runs"))
    assert result.run_result.status is RunStatus.COMPLETED
    assert result.verifier_result is not None and result.verifier_result.passed
    assert result.run_config.model == "langgraph_ref:cassette:scripted"


def test_reference_graph_failure_gets_a_full_bundle_with_a_root_cause(tmp_path, capsys):
    runs = tmp_path / "runs"
    argv = ["run-pipeline", str(FAILURE_TASK_PATH), "--agent", REF, "--runs-dir", str(runs)]
    assert main(argv) == 0
    (run_dir,) = [p for p in runs.iterdir() if p.is_dir()]
    attribution = json.loads((run_dir / names.ATTRIBUTION_RESULT).read_text())
    assert attribution["root_cause_step"] == 3
    assert attribution["first_irreversible_action_step"] == 5
    for artifact in (names.FAILURE_CARD, names.REPAIR_PACKAGE, names.REGRESSION_ARTIFACT):
        assert (run_dir / artifact).is_file()
    trace = [json.loads(line) for line in (run_dir / names.TRACE).read_text().splitlines()]
    responses = [e for e in trace if e["event_type"] == "model_response"]
    # Each graph turn's AIMessage is forwarded, recorded before that step's move.
    assert [e["step_id"] for e in responses] == list(range(1, 8))
    assert all(e["payload"]["raw"]["type"] == "ai" for e in responses)


@pytest.mark.parametrize("task_path", [VALID_TASK_PATH, FAILURE_TASK_PATH], ids=lambda p: p.stem)
def test_committed_cassettes_are_the_scripted_recording(task_path, tmp_path):
    record_cassette(
        langgraph_ref.LangGraphReferenceAgent,
        task_path,
        namespace=langgraph_ref.NAMESPACE,
        root=tmp_path / "cassettes",
        runs_dir=tmp_path / "runs",
    )
    task_id = load_task(task_path).task_id
    fresh = CassetteTurns(langgraph_ref.NAMESPACE, root=tmp_path / "cassettes").path(task_id)
    committed = CassetteTurns(langgraph_ref.NAMESPACE, root=COMMITTED).path(task_id)
    assert fresh.read_bytes() == committed.read_bytes()


def test_the_graph_sends_its_model_the_runners_own_transcript(tmp_path):
    """Step for step, the model sees what the harness runner builds for a fixture model."""
    assert (
        main(
            [
                "run-fixture",
                str(FAILURE_TASK_PATH),
                "--cassette-mode",
                "record",
                "--cassette-dir",
                str(tmp_path / "native"),
                "--runs-dir",
                str(tmp_path / "runs"),
            ]
        )
        == 0
    )
    (native_path,) = (tmp_path / "native").rglob("*.jsonl")
    native = [json.loads(line) for line in native_path.read_text().splitlines()]
    graph_path = CassetteTurns(langgraph_ref.NAMESPACE, root=COMMITTED).path(
        "refund_policy_failure"
    )
    graph = [json.loads(line) for line in graph_path.read_text().splitlines()]
    assert len(graph) == len(native) == 7
    for ours, theirs in zip(graph, native, strict=True):
        assert ours["transcript_hash"] == theirs["transcript_hash"]
        assert ours["response"] == theirs["response"]


def test_installed_control_blocks_the_graphs_refund_and_its_model_sees_why(tmp_path):
    seen = []

    class Spy(ScriptedTurns):
        def __call__(self, prompt):
            adapter = super().__call__(prompt)
            original = adapter.next_action

            def next_action(transcript, tools):
                seen.append(list(transcript))
                return original(transcript, tools)

            adapter.next_action = next_action
            return adapter

    agent = langgraph_ref.LangGraphReferenceAgent(Spy())
    result, trace, store = _run(agent, FAILURE_TASK_PATH, tmp_path, controls=reference_controls())

    executed = [e for e in _events(trace, TraceEventType.TOOL_CALL_EXECUTED) if e.step_id == 5]
    observed = [e for e in _events(trace, TraceEventType.TOOL_OBSERVATION) if e.step_id == 5]
    assert executed[0].payload["tool_name"] == "issue_refund"
    assert executed[0].payload["blocked_by"] == REFUND_WINDOW_CONTROL_ID
    assert observed[0].payload["blocked_by"] == REFUND_WINDOW_CONTROL_ID
    assert store.read_json(result.run_id, names.FINAL_STATE)["refunds"] == []
    # The model's sixth request ends with the blocked refund's tool message.
    last = seen[5][-1]
    assert last.role is MessageRole.TOOL
    assert last.metadata["status"] == "error"
    assert last.metadata["error"] == observed[0].payload["error"]


def test_cassette_replay_stops_where_a_control_changes_the_conversation(tmp_path):
    result, trace, _ = _run(
        langgraph_ref.agent(), FAILURE_TASK_PATH, tmp_path, controls=reference_controls()
    )
    # The block itself is still recorded before the replay notices the drift.
    executed = [e for e in _events(trace, TraceEventType.TOOL_CALL_EXECUTED) if e.step_id == 5]
    assert executed[0].payload["blocked_by"] == REFUND_WINDOW_CONTROL_ID
    assert result.termination_reason is TerminationReason.MODEL_ERROR
    assert "cassette request mismatch at step 6" in (result.error or "")


def test_unwired_graph_records_moves_without_reasoning_and_attribution_goes_null(tmp_path):
    agent = langgraph_ref.LangGraphReferenceAgent(
        CassetteTurns(langgraph_ref.NAMESPACE), forward_model_responses=False
    )
    result, trace, store = _run(agent, FAILURE_TASK_PATH, tmp_path)

    assert result.status is RunStatus.COMPLETED
    assert _events(trace, TraceEventType.MODEL_RESPONSE) == []
    assert all(e.payload["reasoning"] is None for e in _events(trace, TraceEventType.MODEL_ACTION))
    task = load_task(FAILURE_TASK_PATH)
    verdict = get_verifier("refund_policy").verify(
        VerifierInput.from_parts(
            task=task,
            trace=trace,
            final_state=store.read_json(result.run_id, names.FINAL_STATE),
            run_id=result.run_id,
        )
    )
    attribution = HeuristicAttributor().attribute(task, trace, verdict)
    assert attribution.root_cause_step is None
    assert attribution.first_irreversible_action_step == 5


def test_harness_step_limit_binds_before_the_graph_recursion_limit(tmp_path):
    agent = langgraph_ref.scripted_agent()
    result, trace, _ = _run(agent, FAILURE_TASK_PATH, tmp_path, max_steps=3)
    assert result.termination_reason is TerminationReason.MAX_STEPS_REACHED
    assert result.steps_taken == 3
    assert len(_events(trace, TraceEventType.TOOL_CALL_EXECUTED)) == 3


def test_turns_round_trip_through_langchain_messages():
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

    from trace_harness.agents.turns import assistant_message, observation_text
    from trace_harness.models.base import ActionKind, AgentAction, ToolCall
    from trace_harness.runner.target_agent import ToolObservation

    call = AgentAction(
        kind=ActionKind.TOOL_CALL,
        tool_call=ToolCall(tool_name="get_order", arguments={"customer_name": "Riley Chen"}),
        reasoning="checking the order first",
        provider_state={"thought_signature": "opaque"},
    )
    answer = AgentAction(kind=ActionKind.FINAL_ANSWER, final_answer="done", reasoning="wrap up")
    observation = ToolObservation(tool_name="get_order", status="error", error="no order")
    transcript = langgraph_ref.transcript_of(
        [
            SystemMessage("system"),
            HumanMessage("user"),
            langgraph_ref.ai_message(call, call_id="call_3"),
            ToolMessage(observation_text(observation), tool_call_id="call_3", name="get_order"),
            langgraph_ref.ai_message(answer, call_id="call_5"),
        ]
    )
    assert transcript[2] == assistant_message(call)
    assert transcript[3].metadata == {
        "tool_name": "get_order",
        "status": "error",
        "result": {},
        "error": "no order",
    }
    assert transcript[4] == assistant_message(answer)
    # Text beside a tool call is reasoning; text on its own is the answer.
    narrated = AIMessage(content="looking it up", tool_calls=[{"name": "x", "args": {}, "id": "1"}])
    assert langgraph_ref.reasoning_of(narrated) == "looking it up"
    assert langgraph_ref.reasoning_of(AIMessage(content="the answer")) is None
