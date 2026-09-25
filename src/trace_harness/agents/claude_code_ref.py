"""Reference outside agent that runs the task through the local Claude Code CLI.

The agent loop is Claude Code's own. The harness keeps the environment, the
controls, the trace and the verifier, as it does for every outside agent, and
the CLI's model calls run on whatever the CLI is logged in with. Logged in with
a Claude subscription, a run counts against that plan instead of being billed
per token.

How one run works
    :meth:`ClaudeCodeAgent.run` starts ``claude -p`` in a fresh temporary
    directory with

    - ``--tools ""``, so no built-in tool exists, and an MCP server
      (``claude_code_mcp.py``) as the only tool source, under
      ``--strict-mcp-config``. The server lists exactly the task's tools, and
      every call it receives is relayed over a Unix socket back to this
      process and run through the bridge's ``call_tool``. A blocked call comes
      back to the CLI as the control's message, with ``isError`` set.
    - the task's tools allowed by name and ``--permission-mode dontAsk`` with
      ``--permission-prompts none``, so nothing can wait on a permission
      prompt, and ``--setting-sources ""``, so no user or project settings,
      hooks or CLAUDE.md load.
    - ``--system-prompt`` built from the task prompt the bridge provides, with
      one line saying how the task's tools are named in the CLI.
    - ``--no-session-persistence``, ``--output-format stream-json`` and
      ``--model``.

    The ``system/init`` message is checked before anything else counts: the
    tools must be exactly the task's, the harness MCP server must be
    connected, the model must be the one asked for, and ``apiKeySource`` must
    be ``none``, which is how the CLI reports a run on a Claude login instead
    of an API key. The environment the CLI starts with has no
    ``ANTHROPIC_API_KEY`` or ``ANTHROPIC_AUTH_TOKEN`` and no variable that
    marks a nested Claude Code session.

    Every assistant message in the stream is forwarded to
    ``on_model_response``. The CLI writes one message per content block, so
    the blocks of one model response (one message id) are forwarded together,
    thinking blocks without their signatures. Thinking, and any text written
    beside a tool call, become the reasoning. A tool call waits until its
    ``tool_use`` block has been read from the stream, so the response that
    made the call lands on the call's own step. The ``result`` message's
    usage, per-model usage and ``total_cost_usd`` are forwarded last, beside
    the CLI version and model, so the trace records them at the final step.
    ``total_cost_usd`` goes out a second time as ``notional_cost_usd``, which
    a batch records apart from ``cost_usd``: on a plan it is what the calls
    would have cost over the API, and nothing was charged.

    Failures end the run as a :class:`ClaudeCodeError`, which the bridge turns
    into ``model_error``: no ``claude`` on PATH, a missing login, an error
    result, a non-zero exit, output that is not JSON, the usage limit of the
    plan, a model other than the one asked for, and a run that outlives its
    time limit. The CLI runs in its own process group, which is stopped with
    everything in it (the MCP server included) whenever a run ends, also when
    the harness run ended first, and at interpreter exit.

Offline replay
    With a :class:`ClaudeCodeCassette` in ``record`` mode, each move (a tool
    call or the final answer) is written as one entry of a harness model
    cassette (``models/cassette.py``) at
    ``<root>/claude_code_ref/<task_id>/<model>/default.jsonl``. The entry's
    ``provider_state`` keeps the responses forwarded for that move, so a
    replay forwards the same payloads. In ``replay`` mode no CLI is started.
    Each move is served from the cassette, which checks that the conversation
    so far, meaning the opening messages and every earlier move and its
    observation, matches the recording, and a run that drifts from it stops
    with a request mismatch at that step.

Rate limits
    A ``rate_limit_event`` whose status is ``rejected`` means the plan's usage
    limit was reached. The run is stopped at once and its error says when
    the limit resets, instead of the CLI waiting it out.

``--agent trace_harness.agents.claude_code_ref:agent`` runs claude-sonnet-5.
The module needs nothing beyond the core package and a ``claude`` on PATH that
is logged in (https://code.claude.com/docs/en/headless).
"""

from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from trace_harness.agents.turns import CASSETTE_ROOT, assistant_message, observation_text
from trace_harness.environment.tools import ToolResult
from trace_harness.models.base import (
    ActionKind,
    AgentAction,
    Message,
    MessageRole,
    ToolCall,
    ToolSpec,
)
from trace_harness.models.cassette import (
    CassetteRequestConfig,
    RecordingModelAdapter,
    cassette_path,
)
from trace_harness.runner.agent_runner import observation_to_tool_message
from trace_harness.runner.batch import NOTIONAL_COST_KEY
from trace_harness.runner.target_agent import (
    ModelResponseCallback,
    RunEnded,
    TaskPrompt,
    ToolCallback,
    ToolObservation,
)
from trace_harness.secret_scan import SHAPES

NAMESPACE = "claude_code_ref"
DEFAULT_MODEL = "claude-sonnet-5"
MCP_SERVER_NAME = "trace"
#: How the CLI names a tool of the harness MCP server.
TOOL_PREFIX = f"mcp__{MCP_SERVER_NAME}__"
MCP_SERVER_FILE = Path(__file__).with_name("claude_code_mcp.py")
#: Removed from the CLI's environment. The first four mark a process that a
#: Claude Code session started, and the last two would bill an API key
#: instead of the logged-in plan (https://code.claude.com/docs/en/env-vars).
REMOVED_VARIABLES = (
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SSE_PORT",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
)
#: Set in the CLI's environment. Tool search off loads the task's tools up
#: front instead of behind a search tool, auto memory off keeps memory files
#: out of the prompt, and claude.ai connectors off keeps their tools out.
CLI_SETTINGS = {
    "ENABLE_TOOL_SEARCH": "false",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
    "DISABLE_AUTOUPDATER": "1",
}
#: How long after the harness run's time limit the CLI is stopped.
STOP_GRACE_SECONDS = 5.0
#: How long a stopped CLI gets to exit before it is killed.
KILL_GRACE_SECONDS = 3.0

_LIVE: set[subprocess.Popen[bytes]] = set()
_LIVE_LOCK = threading.Lock()


class ClaudeCodeError(RuntimeError):
    """The CLI could not run the task or ended without an answer."""


@dataclass(frozen=True)
class ClaudeCodeCassette:
    """Where one agent records its runs, or replays them from, per task and model."""

    root: Path = CASSETTE_ROOT
    mode: Literal["record", "replay"] = "replay"

    def config(self, task_id: str, model: str) -> CassetteRequestConfig:
        return CassetteRequestConfig(task_id=task_id, provider=NAMESPACE, model=model)

    def path(self, task_id: str, model: str) -> Path:
        return cassette_path(Path(self.root) / NAMESPACE, self.config(task_id, model))


class ClaudeCodeAgent:
    """A TargetAgent whose loop is the Claude Code CLI's.

    ``billing`` says how the CLI's model calls are paid for. It is always
    ``subscription``: the run refuses an API key, so the calls count against
    the logged-in Claude plan and have no per-run charge.
    """

    billing = "subscription"

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        *,
        binary: str = "claude",
        timeout_seconds: float = 900.0,
        cassette: ClaudeCodeCassette | None = None,
        tool_use_wait_seconds: float = 30.0,
    ) -> None:
        self.model = model
        self.binary = binary
        self.timeout_seconds = timeout_seconds
        self.cassette = cassette
        self.tool_use_wait_seconds = tool_use_wait_seconds
        replaying = cassette is not None and cassette.mode == "replay"
        self.name = f"{NAMESPACE}:cassette:{model}" if replaying else f"{NAMESPACE}:{model}"

    def run(
        self,
        prompt: TaskPrompt,
        tools: list[ToolSpec],
        call_tool: ToolCallback,
        on_model_response: ModelResponseCallback | None = None,
    ) -> str:
        if self.cassette is not None and self.cassette.mode == "replay":
            return self._replay(prompt, tools, call_tool, on_model_response)
        return _Session(self, prompt, tools, call_tool, on_model_response).run()

    # --- what the CLI is given ---

    def system_prompt(self, prompt: TaskPrompt) -> str:
        return (
            f"{prompt.system}\n\n"
            f"Each of these tools is available to you as {TOOL_PREFIX}<name>, "
            f"for example {TOOL_PREFIX}get_order."
        )

    def first_message(self, prompt: TaskPrompt) -> str:
        return prompt.user

    def opening(self, prompt: TaskPrompt) -> list[Message]:
        """The conversation the CLI starts from, in the harness transcript shape."""
        return [
            Message(role=MessageRole.SYSTEM, content=self.system_prompt(prompt)),
            Message(role=MessageRole.USER, content=self.first_message(prompt)),
        ]

    def command(
        self, binary: str, prompt: TaskPrompt, tools: list[ToolSpec], mcp_config: Path
    ) -> list[str]:
        """The CLI invocation for one run. The first message goes on stdin."""
        return [
            binary,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            self.model,
            "--system-prompt",
            self.system_prompt(prompt),
            "--tools",
            "",
            "--mcp-config",
            str(mcp_config),
            "--strict-mcp-config",
            "--allowedTools",
            ",".join(TOOL_PREFIX + tool.name for tool in tools),
            "--permission-mode",
            "dontAsk",
            "--permission-prompts",
            "none",
            "--setting-sources",
            "",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--max-turns",
            str(prompt.max_steps + 1),
        ]

    def resolve_binary(self) -> str:
        found = shutil.which(self.binary)
        if found is None:
            raise ClaudeCodeError(
                f"the Claude Code CLI {self.binary!r} is not on PATH; install it "
                "(https://code.claude.com/docs/en/setup) and log in once with `claude`"
            )
        return found

    # --- replay ---

    def _replay(
        self,
        prompt: TaskPrompt,
        tools: list[ToolSpec],
        call_tool: ToolCallback,
        on_model_response: ModelResponseCallback | None,
    ) -> str:
        assert self.cassette is not None
        adapter = RecordingModelAdapter(
            mode="replay",
            path=self.cassette.path(prompt.task_id, self.model),
            config=self.cassette.config(prompt.task_id, self.model),
        )
        transcript = self.opening(prompt)
        while True:
            action = adapter.next_action(transcript, tools)
            for response in (action.provider_state or {}).get("responses", []):
                if on_model_response is not None:
                    on_model_response(response["raw"], response.get("reasoning"))
            if action.kind is ActionKind.FINAL_ANSWER:
                assert action.final_answer is not None
                return action.final_answer
            assert action.tool_call is not None
            call = action.tool_call
            observation = call_tool(call.tool_name, dict(call.arguments))
            transcript += [assistant_message(action), _tool_message(observation)]


class _Recorder:
    """Writes each move of a live run as a cassette entry, through RecordingModelAdapter."""

    name = NAMESPACE

    def __init__(self, path: Path, config: CassetteRequestConfig) -> None:
        self._next: AgentAction | None = None
        self.adapter = RecordingModelAdapter(mode="record", path=path, config=config, inner=self)

    def next_action(self, transcript: list[Message], tools: list[ToolSpec]) -> AgentAction:
        action, self._next = self._next, None
        assert action is not None
        return action

    def record(
        self, transcript: list[Message], tools: list[ToolSpec], action: AgentAction
    ) -> AgentAction:
        """Record ``action`` and return it as a replay would serve it."""
        self._next = action
        return self.adapter.next_action(transcript, tools)


class _Relay:
    """The Unix socket the MCP server forwards the CLI's tool requests to."""

    def __init__(self, path: Path, handler: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        self.path = path
        self.handler = handler
        self._closed = threading.Event()
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(path))
        self._server.listen(16)
        self._server.settimeout(0.2)
        self._thread = threading.Thread(target=self._accept, daemon=True, name="claude-code-relay")
        self._thread.start()

    def _accept(self) -> None:
        while not self._closed.is_set():
            try:
                connection, _ = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=self._serve, args=(connection,), daemon=True).start()

    def _serve(self, connection: socket.socket) -> None:
        with connection:
            connection.settimeout(None)
            received = b""
            while not received.endswith(b"\n"):
                chunk = connection.recv(65536)
                if not chunk:
                    return
                received += chunk
            try:
                request = json.loads(received.decode("utf-8"))
                reply = self.handler(request if isinstance(request, dict) else {})
            except Exception as exc:  # noqa: BLE001 - the CLI sees it as a tool error
                reply = {"text": f"the harness relay failed: {exc}", "is_error": True}
            try:
                connection.sendall(json.dumps(reply).encode("utf-8") + b"\n")
            except OSError:
                pass

    def close(self) -> None:
        self._closed.set()
        self._server.close()
        self._thread.join(2)


class _Session:
    """One live run of the CLI, with the threads and buffers it needs."""

    def __init__(
        self,
        agent: ClaudeCodeAgent,
        prompt: TaskPrompt,
        tools: list[ToolSpec],
        call_tool: ToolCallback,
        on_model_response: ModelResponseCallback | None,
    ) -> None:
        self.agent = agent
        self.prompt = prompt
        self.tools = list(tools)
        self.call_tool = call_tool
        self.on_model_response = on_model_response
        # Guards the stream state below; notified when a tool_use is read.
        self.state = threading.Condition()
        self.group: list[dict[str, Any]] = []
        self.group_id: Any = None
        self.pending_uses: list[tuple[str, Any]] = []
        self.since_move: list[dict[str, Any]] = []
        self.stream_done = False
        # Moves are made one at a time, so the cassette and the bridge see
        # the same order even when the CLI calls tools in parallel.
        self.move_lock = threading.Lock()
        self.transcript = agent.opening(prompt)
        self.recorder: _Recorder | None = None
        self.process: subprocess.Popen[bytes] | None = None
        self.stopped: str | None = None
        self.init: dict[str, Any] = {}
        self.assistant_errors: list[str] = []
        self.rate_limit: dict[str, Any] | None = None
        self.stderr_path: Path | None = None

    # --- lifecycle ---

    def run(self) -> str:
        binary = self.agent.resolve_binary()
        cassette = self.agent.cassette
        if cassette is not None:
            self.recorder = _Recorder(
                cassette.path(self.prompt.task_id, self.agent.model),
                cassette.config(self.prompt.task_id, self.agent.model),
            )
        workdir = _short_tempdir()
        relay: _Relay | None = None
        timer: threading.Timer | None = None
        try:
            relay = _Relay(workdir / "r.sock", self._answer_relay)
            config = workdir / "mcp.json"
            config.write_text(json.dumps(self._mcp_config(relay.path)), encoding="utf-8")
            self.stderr_path = workdir / "stderr.txt"
            command = self.agent.command(binary, self.prompt, self.tools, config)
            with self.stderr_path.open("wb") as stderr:
                self.process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=stderr,
                    cwd=workdir,
                    env=_cli_environment(),
                    start_new_session=True,
                )
            with _LIVE_LOCK:
                _LIVE.add(self.process)
            threading.Thread(target=self._write_first_message, daemon=True).start()
            timer = threading.Timer(self._time_limit(), self._stop, args=("timeout",))
            timer.daemon = True
            timer.start()
            return self._read_stream()
        finally:
            if timer is not None:
                timer.cancel()
            if self.process is not None and self.stopped is None:
                # After its result the CLI exits by itself; give it the chance.
                try:
                    self.process.wait(timeout=KILL_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    pass
            self._stop("finished")
            if relay is not None:
                relay.close()
            if self.process is not None:
                with _LIVE_LOCK:
                    _LIVE.discard(self.process)
            shutil.rmtree(workdir, ignore_errors=True)

    def _time_limit(self) -> float:
        limit = self.agent.timeout_seconds
        if self.prompt.timeout_seconds is not None:
            limit = min(limit, self.prompt.timeout_seconds + STOP_GRACE_SECONDS)
        return limit

    def _mcp_config(self, socket_path: Path) -> dict[str, Any]:
        server = {
            "type": "stdio",
            "command": sys.executable,
            "args": ["-I", str(MCP_SERVER_FILE), str(socket_path)],
            "env": {},
            "alwaysLoad": True,
        }
        return {"mcpServers": {MCP_SERVER_NAME: server}}

    def _write_first_message(self) -> None:
        assert self.process is not None and self.process.stdin is not None
        try:
            self.process.stdin.write(self.agent.first_message(self.prompt).encode("utf-8"))
            self.process.stdin.close()
        except OSError:
            pass  # the CLI exited first; its exit is reported from the stream

    def _stop(self, reason: str) -> None:
        """Stop the CLI and everything it started, and remember why, once."""
        with self.state:
            if self.stopped is None:
                self.stopped = reason
            self.state.notify_all()
        if self.process is not None:
            _stop_group(self.process)

    # --- the stream ---

    def _read_stream(self) -> str:
        assert self.process is not None and self.process.stdout is not None
        result: dict[str, Any] | None = None
        try:
            for number, line in enumerate(self.process.stdout, start=1):
                text = line.decode("utf-8", "replace").strip()
                if not text:
                    continue
                try:
                    message = json.loads(text)
                except ValueError:
                    message = None
                if not isinstance(message, dict):
                    self._stop("malformed")
                    raise ClaudeCodeError(
                        f"line {number} of the CLI's stream-json output is not a JSON "
                        f"object: {_redact(text[:160])!r}"
                    )
                kind = message.get("type")
                if kind == "system" and message.get("subtype") == "init":
                    self._check_init(message)
                elif kind == "assistant":
                    self._on_assistant(message)
                elif kind == "rate_limit_event":
                    self._on_rate_limit(message)
                elif kind == "result":
                    result = message
                    break
        finally:
            with self.state:
                self.stream_done = True
                self.state.notify_all()
        if self.stopped == "run_ended":
            raise RunEnded("the harness run ended while Claude Code was running")
        if self.stopped == "timeout":
            raise ClaudeCodeError(
                f"the Claude Code CLI did not finish within {self._time_limit():.0f} s "
                "and was stopped"
            )
        if self.rate_limit is not None:
            raise ClaudeCodeError(_rate_limit_text(self.rate_limit))
        if result is None:
            code = self._exit_code()
            raise ClaudeCodeError(
                f"the Claude Code CLI exited with status {code} without a result message"
                f"{self._stderr_tail()}"
            )
        return self._final(result)

    def _check_init(self, message: dict[str, Any]) -> None:
        offered = message.get("tools")
        expected = sorted(TOOL_PREFIX + tool.name for tool in self.tools)
        if not isinstance(offered, list) or sorted(offered) != expected:
            offered = offered if isinstance(offered, list) else []
            extra = sorted(set(offered) - set(expected))
            missing = sorted(set(expected) - set(offered))
            self._stop("init")
            raise ClaudeCodeError(
                "Claude Code offered tools other than the task's (extra "
                f"{extra}, missing {missing}); every built-in tool must be disabled"
            )
        servers = {s.get("name"): s.get("status") for s in message.get("mcp_servers") or []}
        if servers.get(MCP_SERVER_NAME) != "connected":
            self._stop("init")
            raise ClaudeCodeError(
                f"the harness MCP server did not connect (status "
                f"{servers.get(MCP_SERVER_NAME)!r}){self._stderr_tail()}"
            )
        source = message.get("apiKeySource")
        if self.agent.billing == "subscription" and source != "none":
            self._stop("init")
            raise ClaudeCodeError(
                f"Claude Code reported apiKeySource {source!r}, so this run would be billed "
                "to an API key; the agent runs only on a Claude login (apiKeySource 'none')"
            )
        model = message.get("model")
        if _base_model(model) != self.agent.model:
            self._stop("init")
            raise ClaudeCodeError(
                f"Claude Code started with model {model!r}, asked for {self.agent.model!r}"
            )
        self.init = {
            "claude_code_version": message.get("claude_code_version"),
            "model": model,
            "permission_mode": message.get("permissionMode"),
        }

    def _on_assistant(self, message: dict[str, Any]) -> None:
        if not self.init:
            self._stop("init")
            raise ClaudeCodeError("Claude Code answered before its system/init message")
        if message.get("parent_tool_use_id"):
            return  # a subagent's message; none can exist without the Agent tool
        body = message.get("message")
        if not isinstance(body, dict):
            return
        model = body.get("model")
        if model not in (None, "<synthetic>") and _base_model(model) != self.agent.model:
            self._stop("model")
            raise ClaudeCodeError(
                f"Claude Code answered with model {model!r}, asked for {self.agent.model!r}"
            )
        if isinstance(message.get("error"), str):
            self.assistant_errors.append(message["error"])
        with self.state:
            if self.group and body.get("id") != self.group_id:
                self._flush()
            self.group_id = body.get("id")
            self.group.append(body)
            for block in body.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    self.pending_uses.append((block.get("name"), block.get("input")))
            self.state.notify_all()

    def _on_rate_limit(self, message: dict[str, Any]) -> None:
        info = message.get("rate_limit_info")
        if isinstance(info, dict) and info.get("status") == "rejected":
            self.rate_limit = info
            self._stop("rate_limited")

    # --- forwarding ---

    def _flush(self) -> None:
        """Forward the buffered model response. Called with ``self.state`` held."""
        if not self.group:
            return
        raw, reasoning = _combine(self.group)
        self.group, self.group_id = [], None
        self._forward(raw, reasoning)

    def _forward(self, raw: dict[str, Any], reasoning: str | None) -> None:
        self.since_move.append({"raw": raw, "reasoning": reasoning})
        if self.on_model_response is not None:
            self.on_model_response(raw, reasoning)

    def _move(self, action: AgentAction) -> AgentAction:
        """Attach the responses forwarded since the last move, and record the move."""
        with self.state:
            self._flush()
            responses, self.since_move = self.since_move, []
        reasoning = "\n\n".join(r["reasoning"] for r in responses if r["reasoning"]) or None
        action = action.model_copy(
            update={
                "reasoning": reasoning,
                "provider_state": {"responses": responses},
                "raw": {"usage": _summed_usage(responses)},
            }
        )
        if self.recorder is not None:
            action = self.recorder.record(self.transcript, self.tools, action)
        return action

    # --- tool calls, on the relay's threads ---

    def _answer_relay(self, request: dict[str, Any]) -> dict[str, Any]:
        if request.get("op") == "list":
            return {
                "tools": [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "inputSchema": tool.parameters or {"type": "object"},
                    }
                    for tool in self.tools
                ]
            }
        if request.get("op") == "call":
            return self._tool_call(str(request.get("name")), request.get("arguments") or {})
        return {"text": f"unknown relay request {request.get('op')!r}", "is_error": True}

    def _tool_call(self, name: str, arguments: Any) -> dict[str, Any]:
        with self.move_lock:
            self._await_tool_use(name, arguments)
            recorded = arguments if isinstance(arguments, dict) else {}
            call = ToolCall(tool_name=name, arguments=recorded)
            action = self._move(AgentAction(kind=ActionKind.TOOL_CALL, tool_call=call))
            try:
                observation = self.call_tool(name, arguments)
            except RunEnded:
                self._stop("run_ended")
                return {"text": "The harness run is over; the call did not run.", "is_error": True}
            self.transcript += [assistant_message(action), _tool_message(observation)]
            return {"text": observation_text(observation), "is_error": observation.status != "ok"}

    def _await_tool_use(self, name: str, arguments: Any) -> None:
        """Wait until the stream has shown the tool_use block behind this call.

        The CLI writes the block to its output before it calls the tool, but
        the two arrive on different pipes. Waiting keeps the response that made
        the call on the call's own step. A block with the same input is taken
        first, then the earliest one for the same tool.
        """
        wanted = TOOL_PREFIX + name
        deadline = time.monotonic() + self.agent.tool_use_wait_seconds
        with self.state:
            while True:
                same_tool = [i for i, (n, _) in enumerate(self.pending_uses) if n == wanted]
                exact = [i for i in same_tool if self.pending_uses[i][1] == arguments]
                if exact or same_tool:
                    del self.pending_uses[(exact or same_tool)[0]]
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self.stream_done or self.stopped:
                    return
                self.state.wait(remaining)

    # --- the end of the run ---

    def _final(self, result: dict[str, Any]) -> str:
        if result.get("is_error") or result.get("subtype") != "success":
            raise ClaudeCodeError(self._error_text(result))
        answer = result.get("result")
        if not isinstance(answer, str):
            raise ClaudeCodeError("the CLI's result message carries no answer text")
        with self.move_lock:
            with self.state:
                self._flush()
            self._forward(_result_summary(result, self.init), None)
            self._move(AgentAction(kind=ActionKind.FINAL_ANSWER, final_answer=answer))
        return answer

    def _error_text(self, result: dict[str, Any]) -> str:
        detail = result.get("result") or "; ".join(str(e) for e in result.get("errors") or [])
        text = (
            f"Claude Code ended with an error result ({result.get('subtype')}"
            f"{', ' + str(result['terminal_reason']) if result.get('terminal_reason') else ''})"
            f": {_redact(str(detail))}"
        )
        if "authentication_failed" in self.assistant_errors or "/login" in str(detail):
            text += (
                ". Claude Code is not logged in: run `claude` in a terminal and log in "
                "with /login. The agent never logs in itself."
            )
        return text

    def _exit_code(self) -> int | None:
        assert self.process is not None
        try:
            return self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._stop("no_result")
            return self.process.wait()

    def _stderr_tail(self) -> str:
        if self.stderr_path is None or not self.stderr_path.exists():
            return ""
        tail = self.stderr_path.read_bytes()[-600:].decode("utf-8", "replace").strip()
        return f"; stderr: {_redact(tail)}" if tail else ""


# --- helpers ---


def _tool_message(observation: ToolObservation) -> Message:
    """An observation in the harness transcript shape, as the runner writes it."""
    result = observation.result if isinstance(observation.result, dict) else {}
    status = observation.status if observation.status in ("ok", "error") else "error"
    return observation_to_tool_message(
        ToolResult(
            tool_name=observation.tool_name, status=status, result=result, error=observation.error
        )
    )


def _combine(group: list[dict[str, Any]]) -> tuple[dict[str, Any], str | None]:
    """The messages of one model response as one payload, and its reasoning.

    Thinking signatures and redacted thinking data are opaque, so they are
    left out. Thinking always counts as reasoning, and text only on a
    response that calls a tool, since on the final one the text is the answer.
    """
    content: list[dict[str, Any]] = []
    for body in group:
        for block in body.get("content") or []:
            if isinstance(block, dict):
                content.append({k: v for k, v in block.items() if k not in ("signature", "data")})
    stop_reasons = [b.get("stop_reason") for b in group if b.get("stop_reason")]
    last = group[-1]
    raw = {
        "type": "assistant",
        "id": last.get("id"),
        "model": last.get("model"),
        "content": content,
        "stop_reason": stop_reasons[-1] if stop_reasons else None,
        "usage": last.get("usage"),
    }
    calls_a_tool = any(block.get("type") == "tool_use" for block in content)
    parts = [str(b.get("thinking") or "") for b in content if b.get("type") == "thinking"]
    if calls_a_tool:
        parts += [str(b.get("text") or "") for b in content if b.get("type") == "text"]
    return raw, "\n\n".join(part for part in parts if part) or None


def _summed_usage(responses: list[dict[str, Any]]) -> dict[str, Any]:
    """Token counts of the model responses behind one move, summed."""
    total: dict[str, Any] = {}
    for response in responses:
        raw = response["raw"]
        usage = raw.get("usage") if raw.get("type") == "assistant" else None
        for key, value in (usage or {}).items():
            if type(value) is int:
                total[key] = total.get(key, 0) + value
            elif isinstance(value, dict):
                inner = total.setdefault(key, {})
                for name, count in value.items():
                    if type(count) is int:
                        inner[name] = inner.get(name, 0) + count
    return total


def _result_summary(result: dict[str, Any], init: dict[str, Any]) -> dict[str, Any]:
    """What the result message says about the run, as forwarded at the final step."""
    keys = (
        "subtype",
        "is_error",
        "num_turns",
        "duration_ms",
        "duration_api_ms",
        "stop_reason",
        "terminal_reason",
        "usage",
        "modelUsage",
        "total_cost_usd",
        "permission_denials",
    )
    summary = {"type": "result", **{k: result[k] for k in keys if k in result}}
    summary.update({k: v for k, v in init.items() if v is not None})
    cost = result.get("total_cost_usd")
    if isinstance(cost, int | float) and not isinstance(cost, bool):
        # On a plan this is what the calls would have cost over the API, and
        # the harness records it apart from cost_usd (runner/batch.py).
        summary[NOTIONAL_COST_KEY] = cost
    return summary


def _base_model(model: Any) -> str | None:
    """A model name without a bracketed variant such as ``[1m]``."""
    return re.sub(r"\[[^\]]*\]$", "", model) if isinstance(model, str) else None


def _rate_limit_text(info: dict[str, Any]) -> str:
    resets = info.get("resetsAt")
    when = ""
    if isinstance(resets, int | float):
        when = f"; it resets at {datetime.fromtimestamp(resets, UTC).isoformat()}"
    return (
        "the Claude plan's usage limit was reached, so the CLI was stopped"
        f"{when} (rate_limit_event {info.get('errorCode') or 'rejected'})"
    )


def _redact(text: str) -> str:
    """Text with anything shaped like a credential replaced."""
    for _, pattern in SHAPES:
        text = pattern.sub("[redacted]", text)
    return text


def _cli_environment() -> dict[str, str]:
    environment = {k: v for k, v in os.environ.items() if k not in REMOVED_VARIABLES}
    environment.update(CLI_SETTINGS)
    return environment


def _short_tempdir() -> Path:
    """A private working directory whose socket path fits the 104-byte AF_UNIX limit."""
    workdir = Path(tempfile.mkdtemp(prefix="tcc-"))
    if len(str(workdir / "r.sock")) > 100:
        shutil.rmtree(workdir, ignore_errors=True)
        workdir = Path(tempfile.mkdtemp(prefix="tcc-", dir="/tmp"))
    return workdir


def _signal_group(process: subprocess.Popen[bytes], sig: int) -> None:
    try:
        os.killpg(process.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _stop_group(process: subprocess.Popen[bytes]) -> None:
    """Stop the CLI politely, then for good, and whatever it left in its group.

    The CLI leads its own process group (``start_new_session``), so the group
    holds the MCP server and anything else it started, and nothing of the
    harness.
    """
    if process.poll() is None:
        _signal_group(process, signal.SIGTERM)
        try:
            process.wait(timeout=KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            _signal_group(process, signal.SIGKILL)
            process.wait()
    _signal_group(process, signal.SIGKILL)


@atexit.register
def _stop_every_cli() -> None:
    with _LIVE_LOCK:
        live = list(_LIVE)
    for process in live:
        if process.poll() is None:
            _stop_group(process)


def agent() -> ClaudeCodeAgent:
    """Claude Code running claude-sonnet-5 on the logged-in Claude plan."""
    return ClaudeCodeAgent(DEFAULT_MODEL)
