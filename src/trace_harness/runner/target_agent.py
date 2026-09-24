"""Run an outside agent through the harness: the TargetAgent protocol and its bridge.

An outside agent (a LangGraph graph, an OpenAI Agents SDK agent, anything with
its own loop) owns its loop and its model calls. The harness owns the
environment, the recorder, and the verifier. The two meet at two callbacks.

    agent.run(prompt, tools, call_tool, on_model_response) -> final answer

``call_tool`` executes one tool call inside the harness environment and returns
what the agent should observe. ``on_model_response`` forwards a raw model
response so the trace can carry it. Everything else about the run is the
harness's business.

How the bridge works
    :class:`TargetAgentBridge` is a :class:`~trace_harness.models.base.ModelAdapter`.
    The agent runs on its own thread; each time it calls ``call_tool`` or returns
    its answer, that move is handed to :class:`~trace_harness.runner.agent_runner.AgentRunner`
    as the next action, and the agent waits until the runner has executed it.
    The runner is unchanged and does what it does for every adapter. It numbers
    steps, validates and executes the call through the environment (so installed
    controls fire at the pre-call seam), records ``tool_call_executed`` and
    ``tool_observation`` with ``blocked_by``, asks the final-answer seam about the
    answer, and enforces the step limit and the time budget. The observation it
    appends to the transcript is what ``call_tool`` returns, blocked message
    included.

    Forwarded model responses are held until the agent's next move and attached
    to it, so the runner records them as ``model_response`` at that step and
    their reasoning lands on the step's ``model_action``. When the agent raises
    instead of moving, they ride on the error and the runner records them at
    the failing step, ahead of the error event. An agent that never calls
    ``on_model_response`` still produces one ``model_action`` per move, with no
    reasoning, which is what lets attribution degrade to nulls.

    Each payload is stored as a JSON round trip of what the agent passed. A
    value JSON cannot hold is written as its string form, a ``raw`` that is not
    a dict is wrapped as ``{"response": raw}``, and several responses before one
    move are stored together as ``{"responses": [...]}``.

What the bridge does not do
    It cannot stop an outside agent's thread. When a run ends early (step limit,
    timeout, a blocked answer), the call the agent is waiting on and every later
    ``call_tool`` raise :class:`RunEnded`, and nothing further reaches the
    environment. That holds even when the runner abandons a move on timeout
    midway, because a tool call is only parked for its result under the lock
    :meth:`TargetAgentBridge.close` takes. Tool calls the agent issues in
    parallel are serialized into consecutive steps in arrival order.

Retries, time and cost (#196)
    The bridge sends no provider request, so the live call policy in
    ``models/policy.py`` never wraps it and ``run_config.json`` records a null
    ``call_policy``. A :class:`TargetAgentError` reaches the runner as
    ``model_error`` on its first occurrence and is never retried, and asking an
    ended agent for another move fails at once instead of waiting out the run's
    time. The runner bounds each move by the run's remaining time, and a move
    spans every model call the agent makes before it. The harness cannot see
    what those calls cost, so the budget guard refuses an outside agent under
    a spend cap as ``budget_unenforceable``.
"""

from __future__ import annotations

import importlib
import json
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from trace_harness.models.base import (
    ActionKind,
    AgentAction,
    Message,
    MessageRole,
    ModelAdapterError,
    ScriptExhaustedError,
    ToolCall,
    ToolSpec,
)
from trace_harness.runner.agent_runner import AgentRunner, ToolEnvironment
from trace_harness.runner.config import RunConfig
from trace_harness.runner.result import RunResult
from trace_harness.tasks.schemas import TaskSpec
from trace_harness.tracing.artifact_store import ArtifactStore

EXTERNAL_PROVIDER = "external"


class TaskPrompt(BaseModel):
    """What an outside agent is asked to do, in the words the harness uses for every agent.

    ``system`` and ``user`` are the two messages ``build_initial_transcript``
    builds for the harness's own adapters. ``max_steps`` is the harness step
    limit, so an agent can size its own recursion or turn limit above it and
    let the harness limit be the one that binds.
    """

    model_config = ConfigDict(frozen=True)

    task_id: str
    system: str
    user: str
    max_steps: int


class ToolObservation(BaseModel):
    """What one tool call returned, as the outside agent sees it.

    A call blocked by an installed control comes back with ``status="error"``
    and the control's message in ``error``. The trace records which control
    blocked it; the agent sees the same message a harness adapter would.
    """

    model_config = ConfigDict(frozen=True)

    tool_name: str
    status: str
    result: Any = None
    error: str | None = None


ToolCallback = Callable[[str, dict[str, Any]], ToolObservation]


class ModelResponseCallback(Protocol):
    def __call__(self, raw: dict[str, Any], reasoning: str | None = None) -> None: ...


@runtime_checkable
class TargetAgent(Protocol):
    """An outside agent the harness can run.

    ``name`` is recorded as the run's model label, so it should say what ran
    (for example ``my-graph:gpt-5``). ``run`` drives the agent to a final
    answer, calling ``call_tool`` for every tool call and, when it can,
    ``on_model_response`` once per model response before acting on it.
    """

    name: str

    def run(
        self,
        prompt: TaskPrompt,
        tools: list[ToolSpec],
        call_tool: ToolCallback,
        on_model_response: ModelResponseCallback | None = None,
    ) -> str: ...


class TargetAgentError(ModelAdapterError):
    """The outside agent raised or returned something other than an answer.

    A subclass of ``ModelAdapterError`` so the runner ends the run with
    ``termination_reason=model_error`` and records the message in the trace.
    """


class RunEnded(RuntimeError):
    """Raised inside ``call_tool`` once the harness run is over."""


@dataclass(frozen=True)
class _ToolMove:
    tool_name: str
    arguments: dict[str, Any]
    # Each call waits on its own reply, so a result can only reach the call
    # that asked for it however many of the agent's threads are calling.
    reply: queue.Queue[Any] = field(default_factory=lambda: queue.Queue(maxsize=1))


@dataclass(frozen=True)
class _FinalMove:
    answer: Any


@dataclass(frozen=True)
class _ErrorMove:
    error: BaseException


_CLOSED = object()


class TargetAgentBridge:
    """Serve an outside agent's moves to ``AgentRunner`` one step at a time.

    Single-run, like the fixture adapter. Use it as a context manager (or call
    :meth:`close`) so an agent still waiting on a tool result is released when
    the run ends.
    """

    name = EXTERNAL_PROVIDER

    def __init__(self, agent: TargetAgent, *, task_id: str, max_steps: int) -> None:
        self.agent = agent
        self.task_id = task_id
        self.max_steps = max_steps
        self._moves: queue.Queue[Any] = queue.Queue()
        # Guards _closed and _pending, so no call posted or parked for a result
        # while close() drains the queue is missed by it.
        self._lock = threading.Lock()
        self._closed = False
        self._responses: list[tuple[dict[str, Any], str | None]] = []
        self._thread: threading.Thread | None = None
        self._pending: _ToolMove | None = None

    # --- ModelAdapter protocol (runner thread) ---

    def next_action(self, transcript: list[Message], tools: list[ToolSpec]) -> AgentAction:
        with self._lock:
            if self._closed:
                raise TargetAgentError("the target agent bridge is closed")
            pending = self._pending
        if self._thread is None:
            self._start(transcript, tools)
        elif pending is not None:
            # Answered before it is cleared, so a transcript that cannot be
            # read leaves the call where close() still finds and releases it.
            _answer(pending, _observation_from(transcript))
            with self._lock:
                if self._pending is pending:
                    self._pending = None
        elif not self._thread.is_alive() and self._moves.empty():
            # The agent's thread posts its last move before it exits, so once
            # that move is taken nothing else will come. Asking again, as a
            # retry would, fails at once instead of waiting out the run's time.
            raise TargetAgentError(
                f"target agent {self.agent.name!r} has ended and has no further move"
            )
        move = self._moves.get()
        if move is _CLOSED:
            raise TargetAgentError("the target agent bridge is closed")
        raw, reasoning = self._drain_responses()
        if isinstance(move, _ErrorMove):
            error = move.error
            failure: ModelAdapterError
            if isinstance(error, ScriptExhaustedError):
                # A scripted model underneath the agent ran out; keep the
                # runner's distinct termination reason for that.
                failure = ScriptExhaustedError(f"target agent {self.agent.name!r}: {error}")
            else:
                failure = TargetAgentError(
                    f"target agent {self.agent.name!r} raised {type(error).__name__}: {error}"
                )
            # Responses the agent forwarded before it raised are part of the
            # run; the runner records them ahead of the error.
            failure.raw = raw
            raise failure from error
        if isinstance(move, _ToolMove):
            with self._lock:
                # close() may have drained the queue after this move was
                # taken (the runner abandons a move on timeout). Parking the
                # call then would leave the agent waiting forever.
                closed = self._closed
                if not closed:
                    self._pending = move
            if closed:
                _answer(move, _CLOSED)
                raise TargetAgentError("the target agent bridge is closed")
            return AgentAction(
                kind=ActionKind.TOOL_CALL,
                tool_call=ToolCall(tool_name=move.tool_name, arguments=move.arguments),
                reasoning=reasoning,
                raw=raw,
            )
        if not isinstance(move.answer, str):
            raise TargetAgentError(
                f"target agent {self.agent.name!r} returned "
                f"{type(move.answer).__name__} instead of a final answer string"
            )
        return AgentAction(
            kind=ActionKind.FINAL_ANSWER, final_answer=move.answer, reasoning=reasoning, raw=raw
        )

    def close(self) -> None:
        """End the run for the agent: release every waiting call and refuse new ones."""
        with self._lock:
            self._closed = True
            waiting = [self._pending] if self._pending is not None else []
            self._pending = None
            while not self._moves.empty():
                move = self._moves.get_nowait()
                if isinstance(move, _ToolMove):
                    waiting.append(move)
            # Wakes a next_action the runner abandoned on timeout.
            self._moves.put(_CLOSED)
        for move in waiting:
            _answer(move, _CLOSED)

    def __enter__(self) -> TargetAgentBridge:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # --- callbacks (agent thread) ---

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> ToolObservation:
        """Execute one tool call in the harness and return what the agent observes."""
        if not isinstance(tool_name, str) or not isinstance(arguments, dict):
            raise TypeError("call_tool takes a tool name string and an arguments dict")
        move = _ToolMove(tool_name, dict(arguments))
        with self._lock:
            if self._closed:
                raise RunEnded("the harness run is over; no further tool calls are executed")
            self._moves.put(move)
        reply = move.reply.get()
        if reply is _CLOSED:
            raise RunEnded("the harness run ended before this tool call's result")
        return reply

    def on_model_response(self, raw: dict[str, Any], reasoning: str | None = None) -> None:
        """Hold a raw model response until the agent's next move."""
        payload = raw if isinstance(raw, dict) else {"response": raw}
        # Round-trip now so a payload the trace cannot store fails here,
        # inside the agent's own call.
        payload = json.loads(json.dumps(payload, default=str))
        text = None if reasoning is None else str(reasoning)
        with self._lock:
            if not self._closed:
                self._responses.append((payload, text))

    # --- internals ---

    def _start(self, transcript: list[Message], tools: list[ToolSpec]) -> None:
        if (
            len(transcript) < 2
            or transcript[0].role is not MessageRole.SYSTEM
            or transcript[1].role is not MessageRole.USER
        ):
            raise TargetAgentError("expected the runner's system and user messages first")
        prompt = TaskPrompt(
            task_id=self.task_id,
            system=transcript[0].content,
            user=transcript[1].content,
            max_steps=self.max_steps,
        )
        self._thread = threading.Thread(
            target=self._run_agent,
            args=(prompt, list(tools)),
            daemon=True,
            name=f"target-agent-{self.task_id}",
        )
        self._thread.start()

    def _run_agent(self, prompt: TaskPrompt, tools: list[ToolSpec]) -> None:
        move: _FinalMove | _ErrorMove
        try:
            move = _FinalMove(self.agent.run(prompt, tools, self.call_tool, self.on_model_response))
        except BaseException as exc:  # noqa: BLE001 - reported to the runner as a model error
            move = _ErrorMove(exc)
        with self._lock:
            # After close nobody is listening, and whatever the agent did once
            # the run was over (including RunEnded) is not part of the run.
            if not self._closed:
                self._moves.put(move)

    def _drain_responses(self) -> tuple[dict[str, Any] | None, str | None]:
        with self._lock:
            responses, self._responses = self._responses, []
        if not responses:
            return None, None
        raw = responses[0][0] if len(responses) == 1 else {"responses": [r for r, _ in responses]}
        reasoning = "\n\n".join(text for _, text in responses if text) or None
        return raw, reasoning


def _answer(move: _ToolMove, reply: Any) -> None:
    """Answer a waiting tool call once.

    close() and a move the runner abandoned can both reach the same call.
    The first answer is the one the agent gets and a later one is dropped.
    """
    try:
        move.reply.put_nowait(reply)
    except queue.Full:
        pass


def _observation_from(transcript: list[Message]) -> ToolObservation:
    """The tool result the runner appended for the previous move."""
    last = transcript[-1] if transcript else None
    if last is None or last.role is not MessageRole.TOOL:
        raise TargetAgentError("expected the runner's tool observation after a tool call")
    return ToolObservation(
        tool_name=last.metadata.get("tool_name", ""),
        status=last.metadata.get("status", "error"),
        result=last.metadata.get("result"),
        error=last.metadata.get("error"),
    )


def load_target_agent(ref: str) -> TargetAgent:
    """Resolve ``package.module:attribute`` to a target agent.

    The attribute may be a target agent instance, a class, or a zero-argument
    factory. Every failure is a ``ValueError`` so the CLI reports it as an
    input error.
    """
    module_name, sep, attribute = ref.partition(":")
    if not sep or not module_name or not attribute:
        raise ValueError(f"agent ref {ref!r} must look like 'package.module:factory'")
    try:
        target: Any = importlib.import_module(module_name)
    except ImportError as exc:
        raise ValueError(f"cannot import agent module {module_name!r}: {exc}") from exc
    for part in attribute.split("."):
        try:
            target = getattr(target, part)
        except AttributeError:
            raise ValueError(f"agent ref {ref!r}: {module_name} has no {attribute!r}") from None
    if isinstance(target, type) or not isinstance(target, TargetAgent):
        if not callable(target):
            raise ValueError(f"agent ref {ref!r} is neither a target agent nor a factory")
        try:
            target = target()
        except TypeError as exc:
            raise ValueError(f"agent ref {ref!r} must take no arguments: {exc}") from exc
    if not isinstance(target, TargetAgent) or not isinstance(target.name, str):
        raise ValueError(f"agent ref {ref!r} did not produce a target agent with name and run")
    return target


def run_target_agent(
    agent: TargetAgent,
    environment: ToolEnvironment,
    store: ArtifactStore,
    task: TaskSpec,
    config: RunConfig,
) -> RunResult:
    """Run ``agent`` on ``task`` through the ordinary runner and close the bridge after."""
    with TargetAgentBridge(agent, task_id=task.task_id, max_steps=config.max_steps) as bridge:
        return AgentRunner(bridge, environment, store).run(task, config)
