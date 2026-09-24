"""The reference outside agents: real framework loops over a scripted model, offline.

Every test runs once per reference agent and skips when that agent's extra is
not installed. Sockets are refused throughout, so a framework that tried to
reach a model or export a trace would fail the test.
"""

from __future__ import annotations

import gc
import importlib.metadata
import json
import runpy
import socket
import sys
import threading
import time
import tomllib
import types
from datetime import UTC, datetime
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

from conftest import FAILURE_TASK_PATH, FIXTURES_DIR, REPO_ROOT, VALID_TASK_PATH
from trace_harness.agents.turns import (
    CassetteTurns,
    ScriptedTurns,
    assistant_message,
    fixture_script_for,
    observation_text,
    record_cassette,
)
from trace_harness.attribution.heuristic import HeuristicAttributor
from trace_harness.cli import main
from trace_harness.environment.controls import REFUND_WINDOW_CONTROL_ID, reference_controls
from trace_harness.environment.support_env import SupportEnvironment
from trace_harness.models.base import ActionKind, AgentAction, MessageRole, ToolCall
from trace_harness.models.fixture import FixtureScript
from trace_harness.runner.config import RunConfig
from trace_harness.runner.pipeline import run_task_pipeline
from trace_harness.runner.result import RunResult, RunStatus, TerminationReason
from trace_harness.runner.suite import AgentConfig
from trace_harness.runner.target_agent import TaskPrompt, ToolObservation, run_target_agent
from trace_harness.tasks.loader import load_docs_for_task, load_task
from trace_harness.tracing import artifact_store as names
from trace_harness.tracing.artifact_store import ArtifactStore
from trace_harness.tracing.events import TraceEventType
from trace_harness.verifiers.base import VerifierInput
from trace_harness.verifiers.registry import get_verifier

COMMITTED = REPO_ROOT / "fixtures" / "cassettes"
EXTRAS = {"langgraph_ref": "langgraph", "openai_agents_ref": "openai-agents"}


def _import(name: str):
    return pytest.importorskip(
        f"trace_harness.agents.{name}",
        reason=f"needs the {EXTRAS[name]} extra",
        exc_type=ImportError,
    )


@pytest.fixture(params=sorted(EXTRAS))
def reference(request):
    return _import(request.param)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("a reference agent attempted to use the network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    for variable in ("OPENAI_API_KEY", "LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2"):
        monkeypatch.delenv(variable, raising=False)


def _make(reference, turns, **kwargs):
    return type(reference.scripted_agent())(turns, **kwargs)


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


def test_reference_agent_passes_the_valid_cash_task(reference, tmp_path):
    ref = f"{reference.__name__}:agent"
    external = AgentConfig(label="ref", provider="external", agent_ref=ref)
    result = run_task_pipeline(VALID_TASK_PATH, external, ArtifactStore(tmp_path / "runs"))
    assert result.run_result.status is RunStatus.COMPLETED
    assert result.verifier_result is not None and result.verifier_result.passed
    assert result.run_config.model == f"{reference.NAMESPACE}:cassette:scripted"


def test_reference_failure_gets_a_full_bundle_with_a_root_cause(reference, tmp_path, capsys):
    runs = tmp_path / "runs"
    ref = f"{reference.__name__}:agent"
    assert (
        main(["run-pipeline", str(FAILURE_TASK_PATH), "--agent", ref, "--runs-dir", str(runs)]) == 0
    )
    (run_dir,) = [p for p in runs.iterdir() if p.is_dir()]
    attribution = json.loads((run_dir / names.ATTRIBUTION_RESULT).read_text())
    assert attribution["root_cause_step"] == 3
    assert attribution["first_irreversible_action_step"] == 5
    for artifact in (names.FAILURE_CARD, names.REPAIR_PACKAGE, names.REGRESSION_ARTIFACT):
        assert (run_dir / artifact).is_file()
    regression = json.loads((run_dir / names.REGRESSION_ARTIFACT).read_text())
    assert regression["replay_command"].endswith(f"--agent {ref}")
    trace = [json.loads(line) for line in (run_dir / names.TRACE).read_text().splitlines()]
    responses = [e for e in trace if e["event_type"] == "model_response"]
    # Every model response is forwarded and recorded before that step's move.
    assert [e["step_id"] for e in responses] == list(range(1, 8))


@pytest.mark.parametrize("task_path", [VALID_TASK_PATH, FAILURE_TASK_PATH], ids=lambda p: p.stem)
def test_committed_cassettes_are_the_scripted_recording(reference, task_path, tmp_path):
    record_cassette(
        type(reference.scripted_agent()),
        task_path,
        namespace=reference.NAMESPACE,
        root=tmp_path / "cassettes",
        runs_dir=tmp_path / "runs",
    )
    task_id = load_task(task_path).task_id
    fresh = CassetteTurns(reference.NAMESPACE, root=tmp_path / "cassettes").path(task_id)
    committed = CassetteTurns(reference.NAMESPACE, root=COMMITTED).path(task_id)
    assert fresh.read_bytes() == committed.read_bytes()


def _prompt(task_id: str) -> TaskPrompt:
    return TaskPrompt(task_id=task_id, system="system", user="user", max_steps=16)


def test_scripted_turns_play_the_script_the_task_names(tmp_path):
    """The scripted source plays metadata.fixture_script, as the fixture provider does."""
    failure_script = FIXTURES_DIR / "scripts" / "refund_policy_failure_script.json"
    tasks = tmp_path / "tasks" / "nested"
    tasks.mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "any_name.json").write_bytes(failure_script.read_bytes())
    task = json.loads(VALID_TASK_PATH.read_text(encoding="utf-8"))
    task["metadata"]["fixture_script"] = "../../elsewhere/any_name.json"
    (tasks / "task.json").write_text(json.dumps(task), encoding="utf-8")

    found = fixture_script_for("refund_policy_valid_cash", tmp_path / "tasks")
    assert found == (tmp_path / "elsewhere" / "any_name.json").resolve()
    adapter = ScriptedTurns(tasks_dir=tmp_path / "tasks")(_prompt("refund_policy_valid_cash"))
    assert adapter.script == FixtureScript.model_validate_json(failure_script.read_text())
    with pytest.raises(FileNotFoundError, match="no task file with task_id 'nope'"):
        fixture_script_for("nope", tmp_path / "tasks")


@pytest.mark.parametrize("task_path", [VALID_TASK_PATH, FAILURE_TASK_PATH], ids=lambda p: p.stem)
def test_every_committed_task_resolves_to_the_fixture_providers_script(task_path):
    task = load_task(task_path)
    expected = (task_path.parent / task.metadata["fixture_script"]).resolve()
    assert fixture_script_for(task.task_id) == expected


def test_default_fixture_paths_do_not_depend_on_the_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    adapter = ScriptedTurns()(_prompt("refund_policy_failure"))
    assert adapter.script.task_id == "refund_policy_failure"
    for namespace in EXTRAS:
        assert CassetteTurns(namespace).path("refund_policy_failure").is_file()


@pytest.mark.parametrize("factory", ["agent", "scripted_agent"])
def test_reference_factories_run_from_any_directory(reference, factory, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    agent = getattr(reference, factory)()
    result, _, _ = _run(agent, FAILURE_TASK_PATH, tmp_path)
    assert result.status is RunStatus.COMPLETED, result.error
    assert result.steps_taken == 7


RECORD_SCRIPT = REPO_ROOT / "scripts" / "record_reference_cassettes.py"


def test_the_record_script_exits_nonzero_when_a_recording_does_not_complete(
    tmp_path, monkeypatch, capsys
):
    main_ = runpy.run_path(str(RECORD_SCRIPT))["main"]
    stub = types.ModuleType("trace_harness.agents.langgraph_ref")
    stub.NAMESPACE = "langgraph_ref"
    stub.LangGraphReferenceAgent = object
    monkeypatch.setitem(sys.modules, stub.__name__, stub)
    outcomes = iter(
        [
            (RunStatus.COMPLETED, TerminationReason.FINAL_ANSWER),
            (RunStatus.ERROR, TerminationReason.MODEL_ERROR),
        ]
    )

    def fake_record(make_agent, task, **kwargs):
        status, reason = next(outcomes)
        now = datetime.now(UTC)
        return RunResult(
            run_id="run_x",
            task_id=Path(task).stem,
            status=status,
            termination_reason=reason,
            steps_taken=1,
            started_at=now,
            finished_at=now,
        )

    monkeypatch.setitem(main_.__globals__, "record_cassette", fake_record)
    assert main_(["langgraph_ref", "--root", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "refund_policy_failure.json" in err
    assert "refund_policy_valid_cash.json" not in err


def test_recording_into_a_used_root_fails_the_record_script(reference, tmp_path, capsys):
    main_ = runpy.run_path(str(RECORD_SCRIPT))["main"]
    argv = [reference.NAMESPACE, "--root", str(tmp_path), str(VALID_TASK_PATH)]
    assert main_(argv) == 0
    # Recording never overwrites, so the second run cannot record and must say so.
    assert main_(argv) == 1
    assert "model_error" in capsys.readouterr().out


TASK_PATHS = sorted(
    path
    for path in (REPO_ROOT / "fixtures" / "tasks").rglob("*.json")
    if "counterexamples" not in path.parts
)


@pytest.mark.parametrize("task_path", TASK_PATHS, ids=lambda p: p.stem)
def test_every_task_ends_as_it_does_under_the_fixture_provider(reference, task_path, tmp_path):
    """The framework loop adds nothing and loses nothing the verifier can see."""
    fixture = run_task_pipeline(
        task_path, AgentConfig(label="fixture"), ArtifactStore(tmp_path / "f"), bundle_on_fail=False
    )
    ref = f"{reference.__name__}:scripted_agent"
    external = run_task_pipeline(
        task_path,
        AgentConfig(label="ref", provider="external", agent_ref=ref),
        ArtifactStore(tmp_path / "e"),
        bundle_on_fail=False,
    )
    assert external.run_result.termination_reason == fixture.run_result.termination_reason
    assert external.run_result.steps_taken == fixture.run_result.steps_taken
    assert external.verifier_result.verdict == fixture.verifier_result.verdict
    assert [(c.check_id, c.step_ids) for c in external.verifier_result.failed_checks] == [
        (c.check_id, c.step_ids) for c in fixture.verifier_result.failed_checks
    ]


@pytest.mark.parametrize(
    ("task_path", "steps"), [(VALID_TASK_PATH, 5), (FAILURE_TASK_PATH, 7)], ids=["valid", "failure"]
)
def test_the_agent_sends_its_model_the_runners_own_transcript(
    reference, task_path, steps, tmp_path
):
    """Step for step, the model sees what the harness runner builds for a fixture model."""
    argv = [
        "run-fixture",
        str(task_path),
        "--cassette-mode",
        "record",
        "--cassette-dir",
        str(tmp_path / "native"),
        "--runs-dir",
        str(tmp_path / "runs"),
    ]
    assert main(argv) == 0
    (native_path,) = (tmp_path / "native").rglob("*.jsonl")
    native = [json.loads(line) for line in native_path.read_text().splitlines()]
    path = CassetteTurns(reference.NAMESPACE, root=COMMITTED).path(load_task(task_path).task_id)
    ours = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(ours) == len(native) == steps
    for mine, theirs in zip(ours, native, strict=True):
        assert mine["transcript_hash"] == theirs["transcript_hash"]
        assert mine["response"] == theirs["response"]
        # The Agents SDK passes tool schemas through untouched; LangChain
        # rewrites them when binding, so only the transcripts match there.
        if reference.NAMESPACE == "openai_agents_ref":
            assert mine["tools_hash"] == theirs["tools_hash"]


def test_installed_control_blocks_the_refund_and_the_model_sees_why(reference, tmp_path):
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

    agent = _make(reference, Spy())
    result, trace, store = _run(agent, FAILURE_TASK_PATH, tmp_path, controls=reference_controls())

    executed = [e for e in _events(trace, TraceEventType.TOOL_CALL_EXECUTED) if e.step_id == 5]
    observed = [e for e in _events(trace, TraceEventType.TOOL_OBSERVATION) if e.step_id == 5]
    assert executed[0].payload["tool_name"] == "issue_refund"
    assert executed[0].payload["blocked_by"] == REFUND_WINDOW_CONTROL_ID
    assert observed[0].payload["blocked_by"] == REFUND_WINDOW_CONTROL_ID
    assert store.read_json(result.run_id, names.FINAL_STATE)["refunds"] == []
    # The model's sixth request ends with the blocked refund's tool result.
    last = seen[5][-1]
    assert last.role is MessageRole.TOOL
    assert last.metadata["status"] == "error"
    assert last.metadata["error"] == observed[0].payload["error"]


def test_cassette_replay_stops_where_a_control_changes_the_conversation(reference, tmp_path):
    agent = reference.agent()
    result, trace, _ = _run(agent, FAILURE_TASK_PATH, tmp_path, controls=reference_controls())
    # The block itself is still recorded before the replay notices the drift.
    executed = [e for e in _events(trace, TraceEventType.TOOL_CALL_EXECUTED) if e.step_id == 5]
    assert executed[0].payload["blocked_by"] == REFUND_WINDOW_CONTROL_ID
    assert result.termination_reason is TerminationReason.MODEL_ERROR
    assert "cassette request mismatch at step 6" in (result.error or "")


def test_unwired_agent_records_moves_without_reasoning_and_attribution_goes_null(
    reference, tmp_path
):
    agent = _make(reference, CassetteTurns(reference.NAMESPACE), forward_model_responses=False)
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


def test_harness_step_limit_binds_before_the_frameworks_own_limit(reference, tmp_path):
    agent = reference.scripted_agent()
    result, trace, _ = _run(agent, FAILURE_TASK_PATH, tmp_path, max_steps=3)
    assert result.termination_reason is TerminationReason.MAX_STEPS_REACHED
    assert result.steps_taken == 3
    assert len(_events(trace, TraceEventType.TOOL_CALL_EXECUTED)) == 3


class _Turns:
    """A turn source that plays the given actions in order, for every task."""

    label = "listed"

    def __init__(self, *actions: AgentAction) -> None:
        self.actions = actions

    def __call__(self, prompt):
        remaining = list(self.actions)

        class Adapter:
            def next_action(self, transcript, tools):
                return remaining.pop(0)

        return Adapter()


def test_the_framework_handles_an_unknown_tool_before_the_harness_sees_it(reference, tmp_path):
    """What the guide says each reference framework does with a tool name it was not given."""
    unknown = AgentAction(
        kind=ActionKind.TOOL_CALL, tool_call=ToolCall(tool_name="wire_money", arguments={})
    )
    answer = AgentAction(kind=ActionKind.FINAL_ANSWER, final_answer="done")
    agent = _make(reference, _Turns(unknown, answer))
    result, trace, _ = _run(agent, VALID_TASK_PATH, tmp_path)

    # The call never reaches call_tool, so no tool event records it.
    assert _events(trace, TraceEventType.TOOL_CALL_REQUESTED) == []
    assert _events(trace, TraceEventType.TOOL_CALL_VALIDATED) == []
    # The forwarded model response that made the call is still in the trace.
    (response,) = _events(trace, TraceEventType.MODEL_RESPONSE)
    assert response.step_id == 1
    assert "wire_money" in json.dumps(response.payload["raw"])
    if reference.NAMESPACE == "langgraph_ref":
        # ToolNode answers the model with its own error and the graph goes on.
        assert result.status is RunStatus.COMPLETED
        assert result.steps_taken == 1
        assert len(response.payload["raw"]["responses"]) == 2
    else:
        # The SDK raises, which ends the run as a model error.
        assert result.termination_reason is TerminationReason.MODEL_ERROR
        assert "ModelBehaviorError" in (result.error or "")


def test_arguments_that_miss_the_schema_reach_the_harness(reference, tmp_path):
    bad = AgentAction(
        kind=ActionKind.TOOL_CALL,
        tool_call=ToolCall(tool_name="issue_refund", arguments={"customer_name": "Riley Chen"}),
    )
    answer = AgentAction(kind=ActionKind.FINAL_ANSWER, final_answer="done")
    result, trace, _ = _run(_make(reference, _Turns(bad, answer)), VALID_TASK_PATH, tmp_path)
    assert result.status is RunStatus.COMPLETED
    (validated,) = _events(trace, TraceEventType.TOOL_CALL_VALIDATED)
    assert validated.payload["valid"] is False
    assert _events(trace, TraceEventType.TOOL_CALL_EXECUTED) == []


def test_a_run_leaves_no_framework_threads_behind(reference, tmp_path):
    """Worker threads a framework starts for a run end with it, without waiting for gc."""
    before = set(threading.enumerate())
    gc.disable()
    try:
        for index in range(2):
            result, _, _ = _run(reference.scripted_agent(), VALID_TASK_PATH, tmp_path / str(index))
            assert result.status is RunStatus.COMPLETED
        deadline = time.monotonic() + 5
        left = [t for t in threading.enumerate() if t not in before]
        while left and time.monotonic() < deadline:
            time.sleep(0.05)
            left = [t for t in threading.enumerate() if t not in before and t.is_alive()]
    finally:
        gc.enable()
    assert [t.name for t in left] == []


# --- each framework's message shapes map back to the harness transcript ---

CALL = AgentAction(
    kind=ActionKind.TOOL_CALL,
    tool_call=ToolCall(tool_name="get_order", arguments={"customer_name": "Riley Chen"}),
    reasoning="checking the order first",
)
ANSWER = AgentAction(kind=ActionKind.FINAL_ANSWER, final_answer="done", reasoning="wrap up")
OBSERVATION = ToolObservation(tool_name="get_order", status="error", error="no order")
OBSERVED = {"tool_name": "get_order", "status": "error", "result": {}, "error": "no order"}


def test_turns_round_trip_through_langchain_messages():
    langgraph_ref = _import("langgraph_ref")
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

    call = CALL.model_copy(update={"provider_state": {"thought_signature": "opaque"}})
    transcript = langgraph_ref.transcript_of(
        [
            SystemMessage("system"),
            HumanMessage("user"),
            langgraph_ref.ai_message(call, call_id="call_3"),
            ToolMessage(observation_text(OBSERVATION), tool_call_id="call_3", name="get_order"),
            langgraph_ref.ai_message(ANSWER, call_id="call_5"),
        ]
    )
    assert transcript[2] == assistant_message(call)
    assert transcript[3].metadata == OBSERVED
    assert transcript[4] == assistant_message(ANSWER)
    # Text beside a tool call is reasoning; text on its own is the answer.
    narrated = AIMessage(content="looking it up", tool_calls=[{"name": "x", "args": {}, "id": "1"}])
    assert langgraph_ref.reasoning_of(narrated) == "looking it up"
    assert langgraph_ref.reasoning_of(AIMessage(content="the answer")) is None


def test_turns_round_trip_through_agents_sdk_items():
    openai_agents_ref = _import("openai_agents_ref")

    call_items = openai_agents_ref.output_items(CALL, turn=1)
    answer_items = openai_agents_ref.output_items(ANSWER, turn=4)
    request = [
        {"role": "user", "content": "user"},
        *[item.model_dump(mode="json", exclude_none=True) for item in call_items],
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": observation_text(OBSERVATION),
        },
        *[item.model_dump(mode="json", exclude_none=True) for item in answer_items],
    ]
    transcript = openai_agents_ref.transcript_of("system", request)
    assert [m.role for m in transcript[:2]] == [MessageRole.SYSTEM, MessageRole.USER]
    assert transcript[2] == assistant_message(CALL)
    assert transcript[3].metadata == OBSERVED
    assert transcript[4] == assistant_message(ANSWER)
    assert openai_agents_ref.reasoning_of(call_items) == "checking the order first"
    assert openai_agents_ref.reasoning_of(answer_items) == "wrap up"


def test_agents_sdk_runs_create_no_sdk_traces(tmp_path):
    openai_agents_ref = _import("openai_agents_ref")
    from agents import set_trace_processors
    from agents.tracing import TracingProcessor
    from agents.tracing.processors import default_processor

    started = []

    class Recorder(TracingProcessor):
        def on_trace_start(self, trace):
            started.append(trace)

        def on_trace_end(self, trace): ...
        def on_span_start(self, span): ...
        def on_span_end(self, span): ...
        def shutdown(self): ...
        def force_flush(self): ...

    set_trace_processors([Recorder()])
    try:
        result, _, _ = _run(openai_agents_ref.agent(), VALID_TASK_PATH, tmp_path)
    finally:
        set_trace_processors([default_processor()])
    assert result.status is RunStatus.COMPLETED
    assert started == []


# --- the extras the reference agents install with ---


def _pyproject_requirements() -> dict[str, list[tuple[str, Requirement]]]:
    """Every requirement in pyproject.toml, by distribution, with the group naming it."""
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    groups = {"dependencies": project["dependencies"], **project["optional-dependencies"]}
    by_name: dict[str, list[tuple[str, Requirement]]] = {}
    for group, lines in groups.items():
        for line in lines:
            requirement = Requirement(line)
            by_name.setdefault(canonicalize_name(requirement.name), []).append((group, requirement))
    return by_name


def test_the_openai_agents_extra_names_the_openai_range_its_reference_imports():
    """openai_agents_ref imports openai's response types, and openai-agents 0.22 needs openai 3."""
    ranges = [r.specifier for g, r in _pyproject_requirements()["openai"] if g == "openai-agents"]
    assert len(ranges) == 1
    (openai_range,) = ranges
    assert openai_range.contains("3.0.0") and openai_range.contains("3.19.0")
    assert not openai_range.contains("2.99.0") and not openai_range.contains("4.0.0")
    try:
        installed = importlib.metadata.version("openai-agents")
    except importlib.metadata.PackageNotFoundError:
        return
    # Where the SDK is installed, what it asks of openai sits inside this range.
    (sdk_openai,) = [
        Requirement(line)
        for line in importlib.metadata.requires("openai-agents") or []
        if Requirement(line).name == "openai" and Requirement(line).marker is None
    ]
    assert installed.startswith("0.22.")
    for version in ("2.99.0", "3.0.0", "3.19.0", "4.0.0"):
        assert openai_range.contains(version) == sdk_openai.specifier.contains(version)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "the openai extra still pins openai<2 on this branch. The #229 review widens it to "
        "admit 3.x, and this marker comes off when that change merges"
    ),
)
def test_every_extra_installs_together():
    """No two groups in pyproject.toml ask for versions of one package that cannot coexist.

    ``pip install -e ".[openai,openai-agents]"`` fails otherwise. The ranges
    use only ``>=``, ``<`` and ``==``, so a common version exists exactly when
    one of the bounds (or 0) satisfies every range.
    """
    clashes = []
    for name, entries in _pyproject_requirements().items():
        ranges = [requirement.specifier for _, requirement in entries]
        assert {spec.operator for spec_set in ranges for spec in spec_set} <= {">=", "<", "=="}
        candidates = {Version("0")} | {Version(spec.version) for s in ranges for spec in s}
        if not any(all(r.contains(c, prereleases=True) for r in ranges) for c in candidates):
            clashes.append(f"{name}: " + ", ".join(f"{g} {r.specifier}" for g, r in entries))
    assert clashes == []
