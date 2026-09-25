"""The Claude Code reference agent, driven through a fake ``claude`` on PATH.

No test here runs the real CLI. Each one puts a ``claude`` wrapper around
``tests/fake_claude_cli.py`` alone on PATH, so the real CLI is unreachable and
the suite passes on a machine without it. The fake starts the harness MCP
server the way the CLI does and makes every tool call over MCP, so the relay,
the bridge, the runner, the controls and the trace are all exercised for real.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from conftest import FAILURE_TASK_PATH, FIXTURES_DIR, VALID_TASK_PATH, run_task_fixture
from trace_harness.agents import claude_code_mcp
from trace_harness.agents.claude_code_ref import (
    DEFAULT_MODEL,
    NAMESPACE,
    ClaudeCodeAgent,
    ClaudeCodeCassette,
)
from trace_harness.environment.controls import REFUND_WINDOW_CONTROL_ID, reference_controls
from trace_harness.environment.support_env import SupportEnvironment
from trace_harness.runner.batch import BatchRunner
from trace_harness.runner.config import RunConfig
from trace_harness.runner.result import RunStatus, TerminationReason
from trace_harness.runner.suite import AgentConfig, SuiteSpec
from trace_harness.runner.target_agent import load_target_agent, run_target_agent
from trace_harness.tasks.loader import load_docs_for_task, load_task
from trace_harness.tracing import artifact_store as names
from trace_harness.tracing.artifact_store import ArtifactStore
from trace_harness.tracing.events import TraceEvent, TraceEventType
from trace_harness.verifiers.base import VerifierInput
from trace_harness.verifiers.registry import get_verifier

FAKE = Path(__file__).with_name("fake_claude_cli.py")
SCRIPTS = FIXTURES_DIR / "scripts"
VALID_SCRIPT = SCRIPTS / "refund_policy_valid_cash_script.json"
FAILURE_SCRIPT = SCRIPTS / "refund_policy_failure_script.json"
TASK_TOOLS = ["search_docs", "get_order", "issue_refund", "create_ticket"]


class FakeCli:
    """The scenario the fake plays, and what it recorded while playing it."""

    def __init__(self, directory: Path) -> None:
        self.scenario = directory / "scenario.json"
        self.record = directory / "record.json"

    def play(self, **scenario: Any) -> None:
        for key in ("script",):
            if key in scenario:
                scenario[key] = str(scenario[key])
        self.scenario.write_text(json.dumps({"record": str(self.record), **scenario}))

    def seen(self) -> dict[str, Any]:
        return json.loads(self.record.read_text())


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeCli:
    """A fake ``claude`` alone on PATH, and no network."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    wrapper = bin_dir / "claude"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    cli = FakeCli(tmp_path)
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", str(cli.scenario))

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("the Claude Code agent tried to reach the network")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    return cli


def _run(
    agent: ClaudeCodeAgent,
    task_path: Path,
    runs: Path,
    *,
    controls=(),
    max_steps: int = 16,
    timeout_seconds: float = 120.0,
):
    task = load_task(task_path)
    environment = SupportEnvironment.from_task(task, docs=load_docs_for_task(task, task_path))
    for control in controls:
        environment.install_control(control)
    store = ArtifactStore(runs)
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


def _gone(pid: int, within: float = 15.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    return False


def _refund_window():
    return [c for c in reference_controls() if c.control_id == REFUND_WINDOW_CONTROL_ID]


# --- a run through the CLI ---


def test_a_run_through_the_cli_matches_the_fixture_provider(fake, tmp_path):
    """The same script played through the CLI and the fixture provider gives the same run."""
    fake.play(script=VALID_SCRIPT)
    result, trace, store = _run(ClaudeCodeAgent(), VALID_TASK_PATH, tmp_path / "runs")
    fixture = run_task_fixture(VALID_TASK_PATH, tmp_path / "fixture_runs")

    assert result.status is RunStatus.COMPLETED
    assert (result.steps_taken, result.final_output) == (
        fixture.result.steps_taken,
        fixture.result.final_output,
    )
    skip = {TraceEventType.RUN_STARTED, TraceEventType.MODEL_RESPONSE}
    assert [(e.event_type, e.step_id, e.payload) for e in trace if e.event_type not in skip] == [
        (e.event_type, e.step_id, e.payload) for e in fixture.trace if e.event_type not in skip
    ]
    task = load_task(VALID_TASK_PATH)
    verdict = get_verifier(task.verifier_ids[0]).verify(
        VerifierInput.from_parts(
            task=task,
            trace=trace,
            final_state=store.read_json(result.run_id, names.FINAL_STATE),
            run_id=result.run_id,
        )
    )
    assert verdict.passed


def test_model_responses_carry_reasoning_and_the_result_usage_and_cost(fake, tmp_path):
    fake.play(script=VALID_SCRIPT)
    result, trace, _ = _run(ClaudeCodeAgent(), VALID_TASK_PATH, tmp_path / "runs")

    responses = {e.step_id: e.payload["raw"] for e in _events(trace, TraceEventType.MODEL_RESPONSE)}
    actions = {e.step_id: e.payload for e in _events(trace, TraceEventType.MODEL_ACTION)}
    assert sorted(responses) == list(range(1, result.steps_taken + 1))
    first = responses[1]
    assert first["type"] == "assistant" and first["model"] == DEFAULT_MODEL
    assert [block["type"] for block in first["content"]] == ["text", "tool_use"]
    assert first["content"][1]["name"] == "mcp__trace__search_docs"
    assert actions[1]["reasoning"] == first["content"][0]["text"]
    # The final step forwards the answer's response and then the result message.
    answer, summary = responses[result.steps_taken]["responses"]
    assert [block["type"] for block in answer["content"]] == ["thinking", "text"]
    assert "signature" not in answer["content"][0]
    assert actions[result.steps_taken]["reasoning"] == answer["content"][0]["thinking"]
    assert summary["type"] == "result"
    assert summary["total_cost_usd"] == 0.0123
    assert summary["usage"]["input_tokens"] == 4800
    assert summary["modelUsage"][DEFAULT_MODEL]["costUSD"] == 0.0123
    assert (summary["claude_code_version"], summary["model"]) == ("0.0.0-fake", DEFAULT_MODEL)
    # The same figure again under the key a batch records apart from cost_usd.
    assert summary["notional_cost_usd"] == 0.0123


def test_the_cli_gets_no_built_in_tool_and_exactly_the_task_tools(fake, tmp_path):
    fake.play(script=VALID_SCRIPT)
    _run(ClaudeCodeAgent(), VALID_TASK_PATH, tmp_path / "runs")
    seen = fake.seen()
    argv = seen["argv"]

    def value(flag: str) -> str:
        return argv[argv.index(flag) + 1]

    assert argv[0] == "-p"
    assert value("--tools") == ""
    assert "--strict-mcp-config" in argv
    assert value("--allowedTools") == ",".join(f"mcp__trace__{name}" for name in TASK_TOOLS)
    assert (value("--permission-mode"), value("--permission-prompts")) == ("dontAsk", "none")
    assert value("--setting-sources") == ""
    assert "--no-session-persistence" in argv
    assert (value("--model"), value("--output-format")) == (DEFAULT_MODEL, "stream-json")
    assert value("--system-prompt").startswith("You are a support agent")
    assert [tool["name"] for tool in seen["listed_tools"]] == TASK_TOOLS
    # Tool search off, so the tools load up front and no search tool is added.
    assert seen["tool_search"] == "false"
    # The run happens in its own scratch folder, which is gone afterwards.
    assert not Path(seen["cwd"]).exists()


@pytest.mark.parametrize("extra", [["Bash"], ["ToolSearch", "Read"]])
def test_a_built_in_tool_in_the_session_ends_the_run_before_any_call(fake, tmp_path, extra):
    fake.play(script=VALID_SCRIPT, extra_tools=extra)
    result, trace, _ = _run(ClaudeCodeAgent(), VALID_TASK_PATH, tmp_path / "runs")
    assert result.termination_reason is TerminationReason.MODEL_ERROR
    assert "offered tools other than the task's" in (result.error or "")
    assert all(name in (result.error or "") for name in extra)
    assert not _events(trace, TraceEventType.TOOL_CALL_EXECUTED)


def test_a_blocked_call_reaches_the_cli_as_the_control_message(fake, tmp_path):
    fake.play(script=FAILURE_SCRIPT)
    result, trace, _ = _run(
        ClaudeCodeAgent(), FAILURE_TASK_PATH, tmp_path / "runs", controls=_refund_window()
    )
    assert result.status is RunStatus.COMPLETED
    executed = {e.step_id: e.payload for e in _events(trace, TraceEventType.TOOL_CALL_EXECUTED)}
    assert executed[5]["blocked_by"] == REFUND_WINDOW_CONTROL_ID
    refund = next(r for r in fake.seen()["results"] if r["tool"] == "issue_refund")
    assert refund["isError"] is True
    observation = json.loads(refund["content"][0]["text"])
    assert observation["status"] == "error"
    assert observation["error"].startswith("blocked by refund policy guardrail")
    others = [r for r in fake.seen()["results"] if r["tool"] != "issue_refund"]
    assert others and not any(r["isError"] for r in others)


def test_nested_session_markers_and_api_keys_never_reach_the_cli(fake, tmp_path, monkeypatch):
    for name in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.setenv(name, "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    fake.play(script=VALID_SCRIPT)
    result, _, _ = _run(ClaudeCodeAgent(), VALID_TASK_PATH, tmp_path / "runs")
    assert result.status is RunStatus.COMPLETED
    assert fake.seen()["set_variables"] == ["CLAUDE_CODE_DISABLE_AUTO_MEMORY", "ENABLE_TOOL_SEARCH"]


def test_a_run_billed_to_an_api_key_is_refused(fake, tmp_path):
    fake.play(script=VALID_SCRIPT, api_key_source="ANTHROPIC_API_KEY")
    result, trace, _ = _run(ClaudeCodeAgent(), VALID_TASK_PATH, tmp_path / "runs")
    assert result.termination_reason is TerminationReason.MODEL_ERROR
    assert "billed to an API key" in (result.error or "")
    assert not _events(trace, TraceEventType.TOOL_CALL_EXECUTED)


@pytest.mark.parametrize(
    "scenario",
    [{"init_model": "claude-opus-5"}, {"answer_model": "claude-opus-5"}],
    ids=["session", "answer"],
)
def test_a_model_other_than_the_one_asked_for_ends_the_run(fake, tmp_path, scenario):
    fake.play(script=VALID_SCRIPT, **scenario)
    result, _, _ = _run(ClaudeCodeAgent(), VALID_TASK_PATH, tmp_path / "runs")
    assert result.termination_reason is TerminationReason.MODEL_ERROR
    assert "claude-opus-5" in (result.error or "") and DEFAULT_MODEL in (result.error or "")


def test_a_response_read_after_its_tool_call_arrived_still_lands_on_the_call_step(fake, tmp_path):
    """The stream and the MCP call travel on different pipes, so the call can come first."""
    fake.play(script=VALID_SCRIPT, late_tool_use_seconds=0.5)
    result, trace, _ = _run(ClaudeCodeAgent(), VALID_TASK_PATH, tmp_path / "runs")
    assert result.status is RunStatus.COMPLETED
    responses = {e.step_id: e.payload["raw"] for e in _events(trace, TraceEventType.MODEL_RESPONSE)}
    executed = {e.step_id: e.payload for e in _events(trace, TraceEventType.TOOL_CALL_EXECUTED)}
    for step, call in executed.items():
        (use,) = [b for b in responses[step]["content"] if b["type"] == "tool_use"]
        assert use["name"] == f"mcp__trace__{call['tool_name']}"


def test_parallel_tool_calls_become_consecutive_steps(fake, tmp_path):
    both = [
        {"name": "get_order", "input": {"customer_name": "Riley Chen"}},
        {"name": "search_docs", "input": {"query": "refund policy"}},
    ]
    fake.play(turns=[{"text": "Checking both at once.", "tools": both}, {"final": "Looked."}])
    result, trace, _ = _run(ClaudeCodeAgent(), VALID_TASK_PATH, tmp_path / "runs")
    assert (result.status, result.steps_taken) == (RunStatus.COMPLETED, 3)
    calls = [e.payload["tool_name"] for e in _events(trace, TraceEventType.TOOL_CALL_EXECUTED)]
    assert sorted(calls) == ["get_order", "search_docs"]
    assert not any(r["isError"] for r in fake.seen()["results"])
    # The response that made both calls lands on the first of them.
    (first,) = [e for e in _events(trace, TraceEventType.MODEL_RESPONSE) if e.step_id == 1]
    uses = [b["name"] for b in first.payload["raw"]["content"] if b["type"] == "tool_use"]
    assert sorted(uses) == ["mcp__trace__get_order", "mcp__trace__search_docs"]


def test_a_capped_suite_runs_claude_code_on_the_plan_and_records_its_notional_cost(fake, tmp_path):
    """Subscription billing: admitted under a cap, no charge, the CLI's cost kept apart."""
    fake.play(script=VALID_SCRIPT, total_cost_usd=0.0123)
    config = AgentConfig(
        label="claude-code",
        provider="external",
        agent_ref="trace_harness.agents.claude_code_ref:agent",
        billing="subscription",
    )
    suite = SuiteSpec(
        suite_id="claude_code_capped",
        tasks=[str(VALID_TASK_PATH)],
        agent_configs=[config],
        max_cost_usd=0.01,
    )
    summary = BatchRunner(ArtifactStore(tmp_path / "runs")).run(suite)
    (entry,) = summary.entries
    assert (entry.verdict, entry.model) == ("pass", f"{NAMESPACE}:{DEFAULT_MODEL}")
    assert (entry.cost_usd, entry.notional_cost_usd) == (None, 0.0123)
    # Above the cap as a notional figure, and still no stop, since nothing was charged.
    assert summary.budget is not None
    assert (summary.budget.spent_usd, summary.budget.stop_reason) == (0.0, None)


# --- failures end as clear errors, and leave nothing running ---


FAILURES = {
    "not_logged_in": (
        {
            "turns": [],
            "assistant_error": "authentication_failed",
            "error_result": "Not logged in · Please run /login",
        },
        "is not logged in: run `claude` in a terminal and log in with /login",
    ),
    "error_result": (
        {"turns": [], "error_result": "API Error: overloaded"},
        "ended with an error result (error_during_execution): API Error: overloaded",
    ),
    "exit_without_result": (
        {"turns": [], "exit_without_result": 3},
        "exited with status 3 without a result message",
    ),
    "exit_before_init": (
        {"exit_before_init": 2},
        "exited with status 2 without a result message; stderr: fake claude: something",
    ),
    "malformed_line": (
        {"script": VALID_SCRIPT, "malformed": True},
        "line 2 of the CLI's stream-json output is not a JSON object: 'this line is not json'",
    ),
    "hangs": ({"script": VALID_SCRIPT, "hang_seconds": 60}, "did not finish within 2 s"),
    "usage_limit": (
        {"script": VALID_SCRIPT, "rate_limited": True},
        "the Claude plan's usage limit was reached, so the CLI was stopped; it resets at",
    ),
}


@pytest.mark.parametrize("case", sorted(FAILURES))
def test_a_cli_failure_ends_the_run_as_a_clear_error(fake, tmp_path, case):
    scenario, message = FAILURES[case]
    fake.play(**scenario)
    started = time.monotonic()
    result, trace, _ = _run(ClaudeCodeAgent(timeout_seconds=2), VALID_TASK_PATH, tmp_path / "runs")
    elapsed = time.monotonic() - started

    assert (result.status, result.termination_reason) == (
        RunStatus.ERROR,
        TerminationReason.MODEL_ERROR,
    )
    assert "ClaudeCodeError" in (result.error or "")
    assert message in (result.error or "")
    (error,) = _events(trace, TraceEventType.ERROR)
    assert error.payload["kind"] == "model_error"
    assert elapsed < 20
    seen = fake.seen()
    assert _gone(seen["pid"])
    if "mcp_pid" in seen:
        assert _gone(seen["mcp_pid"])


def test_no_cli_on_path_is_a_clear_error(tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    result, _, _ = _run(ClaudeCodeAgent(), VALID_TASK_PATH, tmp_path / "runs")
    assert result.termination_reason is TerminationReason.MODEL_ERROR
    assert "the Claude Code CLI 'claude' is not on PATH" in (result.error or "")


@pytest.mark.parametrize("limit", ["steps", "time"])
def test_the_cli_is_stopped_when_the_harness_run_ends_first(fake, tmp_path, limit):
    """The harness cannot stop the agent's thread, so the agent stops its own CLI."""
    if limit == "steps":
        fake.play(script=FAILURE_SCRIPT, linger_seconds=120)
        result, _, _ = _run(ClaudeCodeAgent(), FAILURE_TASK_PATH, tmp_path / "runs", max_steps=2)
        assert result.termination_reason is TerminationReason.MAX_STEPS_REACHED
    else:
        fake.play(script=FAILURE_SCRIPT, hang_seconds=120)
        result, _, _ = _run(
            ClaudeCodeAgent(), FAILURE_TASK_PATH, tmp_path / "runs", timeout_seconds=1
        )
        assert result.termination_reason is TerminationReason.TIMEOUT
    seen = fake.seen()
    # By the relay's RunEnded, or by the run's time limit plus a short grace.
    assert _gone(seen["pid"], within=20)
    assert _gone(seen["mcp_pid"], within=20)


# --- offline replay ---


def _comparable(trace: list[TraceEvent]) -> list[tuple[Any, ...]]:
    """Everything a run recorded except its label, ids and times."""
    return [
        (e.event_type, e.step_id, e.payload)
        for e in trace
        if e.event_type is not TraceEventType.RUN_STARTED
    ]


@pytest.mark.parametrize(
    ("task_path", "script", "controls"),
    [(VALID_TASK_PATH, VALID_SCRIPT, False), (FAILURE_TASK_PATH, FAILURE_SCRIPT, True)],
    ids=["valid_cash", "failure_with_control"],
)
def test_cassette_replay_equals_the_recording(
    fake, tmp_path, monkeypatch, task_path, script, controls
):
    root = tmp_path / "cassettes"
    installed = _refund_window() if controls else ()
    fake.play(script=script)
    recorder = ClaudeCodeAgent(cassette=ClaudeCodeCassette(root, "record"))
    recorded, recorded_trace, recorded_store = _run(
        recorder, task_path, tmp_path / "recorded", controls=installed
    )
    assert recorded.status is RunStatus.COMPLETED
    task_id = load_task(task_path).task_id
    cassette = root / NAMESPACE / task_id / DEFAULT_MODEL / "default.jsonl"
    assert len(cassette.read_text().splitlines()) == recorded.steps_taken

    # No CLI at all from here on.
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    replayer = ClaudeCodeAgent(cassette=ClaudeCodeCassette(root, "replay"))
    assert replayer.name == f"{NAMESPACE}:cassette:{DEFAULT_MODEL}"
    runs = []
    for attempt in ("first", "second"):
        replayed, trace, store = _run(replayer, task_path, tmp_path / attempt, controls=installed)
        assert replayed.status is RunStatus.COMPLETED
        assert (replayed.steps_taken, replayed.final_output) == (
            recorded.steps_taken,
            recorded.final_output,
        )
        assert _comparable(trace) == _comparable(recorded_trace)
        assert store.read_json(replayed.run_id, names.FINAL_STATE) == recorded_store.read_json(
            recorded.run_id, names.FINAL_STATE
        )
        runs.append(trace)
    assert _comparable(runs[0]) == _comparable(runs[1])


def test_replay_stops_when_the_run_drifts_from_the_recording(fake, tmp_path, monkeypatch):
    """A control that blocks a call the recording saw succeed changes the next request."""
    root = tmp_path / "cassettes"
    fake.play(script=FAILURE_SCRIPT)
    recorder = ClaudeCodeAgent(cassette=ClaudeCodeCassette(root, "record"))
    _run(recorder, FAILURE_TASK_PATH, tmp_path / "recorded")
    monkeypatch.setenv("PATH", str(tmp_path))
    replayer = ClaudeCodeAgent(cassette=ClaudeCodeCassette(root, "replay"))
    result, trace, _ = _run(
        replayer, FAILURE_TASK_PATH, tmp_path / "replayed", controls=_refund_window()
    )
    assert result.termination_reason is TerminationReason.MODEL_ERROR
    assert "cassette request mismatch at step 6" in (result.error or "")
    executed = {e.step_id: e.payload for e in _events(trace, TraceEventType.TOOL_CALL_EXECUTED)}
    assert executed[5]["blocked_by"] == REFUND_WINDOW_CONTROL_ID


def test_recording_never_overwrites_a_cassette_and_never_starts_the_cli(fake, tmp_path):
    root = tmp_path / "cassettes"
    fake.play(script=VALID_SCRIPT)
    agent = ClaudeCodeAgent(cassette=ClaudeCodeCassette(root, "record"))
    _run(agent, VALID_TASK_PATH, tmp_path / "first")
    fake.record.unlink()
    result, _, _ = _run(agent, VALID_TASK_PATH, tmp_path / "second")
    assert result.termination_reason is TerminationReason.MODEL_ERROR
    assert "cassette already exists" in (result.error or "")
    assert not fake.record.exists()


# --- the MCP server and the agent ref ---


def test_the_mcp_server_speaks_json_rpc_over_stdio():
    """initialize, ping, tools/list and tools/call over the relay; errors for the rest."""
    # pytest's tmp_path is longer than an AF_UNIX path may be on macOS.
    with tempfile.TemporaryDirectory(dir="/tmp") as short:
        _speak_json_rpc(Path(short) / "r.sock")


def _speak_json_rpc(path: Path) -> None:
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(4)
    requests: list[dict[str, Any]] = []

    def relay() -> None:
        for _ in range(2):
            connection, _ = server.accept()
            with connection:
                request = json.loads(connection.makefile().readline())
                requests.append(request)
                if request["op"] == "list":
                    reply = {"tools": [{"name": "get_order", "inputSchema": {"type": "object"}}]}
                else:
                    reply = {"text": '{"status": "error"}', "is_error": True}
                connection.sendall(json.dumps(reply).encode() + b"\n")

    thread = threading.Thread(target=relay, daemon=True)
    thread.start()
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "x"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 4, "method": "resources/list"},
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "get_order", "arguments": {"customer_name": "Riley Chen"}},
        },
    ]
    stdin = "\n".join(json.dumps(line) for line in lines) + "\nnot json\n"
    ran = subprocess.run(
        [sys.executable, "-I", claude_code_mcp.__file__, str(path)],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=30,
    )
    thread.join(5)
    server.close()
    replies = [json.loads(line) for line in ran.stdout.splitlines()]
    by_id = {reply.get("id"): reply for reply in replies}
    assert by_id[1]["result"]["protocolVersion"] == "x"
    assert by_id[1]["result"]["capabilities"] == {"tools": {"listChanged": False}}
    assert by_id[2]["result"] == {}
    assert by_id[3]["result"]["tools"][0]["name"] == "get_order"
    assert by_id[4]["error"]["code"] == -32601
    assert by_id[5]["result"]["isError"] is True
    assert by_id[None]["error"]["code"] == -32700
    assert requests[1] == {
        "op": "call",
        "name": "get_order",
        "arguments": {"customer_name": "Riley Chen"},
    }
    assert len(replies) == 6  # the notification gets no answer


def test_the_agent_ref_runs_claude_sonnet_5_on_the_logged_in_plan():
    agent = load_target_agent("trace_harness.agents.claude_code_ref:agent")
    assert isinstance(agent, ClaudeCodeAgent)
    assert (agent.name, agent.model, agent.billing) == (
        f"{NAMESPACE}:{DEFAULT_MODEL}",
        "claude-sonnet-5",
        "subscription",
    )
