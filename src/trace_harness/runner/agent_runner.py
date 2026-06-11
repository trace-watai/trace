"""AgentRunner: the generic execution loop for one task run.

Composition (none of these are hardcoded to any scenario)::

    AgentRunner
      uses ModelAdapter      (the agent's brain — fixture or real)
      uses ToolEnvironment   (the world — owns tools and mutable state)
      uses TraceRecorder     (created per run; write-through JSONL)
      uses ArtifactStore     (run directory layout)

What the runner does NOT do, on purpose:
    - It never imports tool implementations; the environment owns tools.
    - It never runs verifiers; verification is a separate pipeline stage so
      a trace can be re-verified later without re-running the agent.
    - It never interprets domain state; it snapshots opaque dicts.

Failure behavior
    Every run — including one that crashes — leaves a run directory with
    whatever artifacts were produced. Caught failures record an ``error``
    event and the run still finishes its bookkeeping (final state snapshot,
    ``run_finished``, ``run_result.json``); only a hard kill leaves a trace
    truncated mid-stream. Partial evidence beats no evidence.

# TODO(Rupert/runner): retries/backoff policy for real adapters, and a
# cancellation/timeout that can interrupt a hung provider call (today the
# timeout is only checked between steps).
"""

from __future__ import annotations

import json
import logging
import time
import traceback
import uuid
from typing import Any, Protocol

from trace_harness.environment.tools import ToolResult, ToolSideEffect
from trace_harness.models.base import (
    ActionKind,
    AgentAction,
    Message,
    MessageRole,
    ModelAdapter,
    ModelAdapterError,
    ScriptExhaustedError,
    ToolSpec,
)
from trace_harness.runner.config import RunConfig
from trace_harness.runner.result import RunResult, RunStatus, TerminationReason
from trace_harness.tasks.schemas import TaskSpec
from trace_harness.tracing import artifact_store as names
from trace_harness.tracing.artifact_store import ArtifactStore
from trace_harness.tracing.events import TraceEventType, utc_now
from trace_harness.tracing.recorder import TraceRecorder

logger = logging.getLogger(__name__)


class ToolEnvironment(Protocol):
    """What the runner needs from any environment (structural typing).

    ``SupportEnvironment`` satisfies this today; future environments must
    too. Defined here — at the consumer — so environments never import the
    runner.
    """

    def tool_specs(self) -> list[ToolSpec]: ...

    def validate_call(self, call: Any) -> tuple[bool, str | None]: ...

    def execute(self, call: Any, step_id: int | None = None) -> ToolResult: ...

    def side_effect_for(self, tool_name: str) -> ToolSideEffect | None: ...

    def snapshot_state(self) -> dict[str, Any]: ...


def new_run_id() -> str:
    """Sortable, collision-resistant run id: run_<utc timestamp>_<hex8>."""
    return f"run_{utc_now():%Y%m%dT%H%M%SZ}_{uuid.uuid4().hex[:8]}"


def build_initial_transcript(task: TaskSpec, tools: list[ToolSpec]) -> list[Message]:
    """System + user messages for prompt version ``v0``.

    The fixture adapter ignores these; they exist so traces show what a real
    adapter *would* receive, and so the Gemini adapter starts from a defined
    contract. The inbound request comes from ``metadata.user_message`` with
    ``task.goal`` as fallback.
    """
    tool_lines = "\n".join(f"- {t.name}: {t.description}" for t in tools)
    system = (
        "You are a support agent operating inside a sandboxed environment.\n"
        f"Scenario: {task.description}\n"
        f"Your goal: {task.goal}\n\n"
        "You may use these tools:\n"
        f"{tool_lines}\n\n"
        "Respond with exactly one action per turn: either a tool call or a "
        "final answer to the user."
    )
    user_message = str(task.metadata.get("user_message") or task.goal)
    return [
        Message(role=MessageRole.SYSTEM, content=system),
        Message(role=MessageRole.USER, content=user_message),
    ]


def _action_to_assistant_message(action: AgentAction) -> Message:
    """Serialize an action into the transcript the way a real model turn would land."""
    body: dict[str, Any] = {"kind": action.kind.value}
    if action.reasoning:
        body["reasoning"] = action.reasoning
    if action.tool_call is not None:
        body["tool_call"] = action.tool_call.model_dump(mode="json")
    if action.final_answer is not None:
        body["final_answer"] = action.final_answer
    return Message(role=MessageRole.ASSISTANT, content=json.dumps(body))


def _observation_to_tool_message(result: ToolResult) -> Message:
    body = {
        "tool_name": result.tool_name,
        "status": result.status,
        "result": result.result,
        "error": result.error,
    }
    return Message(role=MessageRole.TOOL, content=json.dumps(body))


class AgentRunner:
    """Runs one task with one adapter inside one environment, recording everything."""

    def __init__(
        self,
        model_adapter: ModelAdapter,
        environment: ToolEnvironment,
        artifact_store: ArtifactStore,
    ):
        self.model_adapter = model_adapter
        self.environment = environment
        self.artifact_store = artifact_store
        self._consumed = False

    def run(self, task: TaskSpec, config: RunConfig) -> RunResult:
        # Fail loud on reuse: the environment's state and a fixture script's
        # cursor are mutated by a run, so a second run() on the same instance
        # would start from a polluted world and an exhausted script.
        if self._consumed:
            raise RuntimeError(
                "AgentRunner instances are single-run: the environment state and "
                "model adapter are stateful. Construct a fresh adapter, "
                "environment, and runner for each run."
            )
        self._consumed = True
        run_id = new_run_id()
        store = self.artifact_store
        store.create_run_dir(run_id)
        recorder = TraceRecorder(run_id, jsonl_path=store.trace_path(run_id))
        started_at = utc_now()
        started_clock = time.monotonic()

        # Snapshot inputs first: even a crash on step 1 leaves a replayable run dir.
        store.write_json(run_id, names.TASK_SPEC, task)
        store.write_json(run_id, names.RUN_CONFIG, config)
        initial_state = self.environment.snapshot_state()
        store.write_json(run_id, names.INITIAL_STATE, initial_state)

        recorder.record(
            TraceEventType.RUN_STARTED,
            payload={
                "task_id": task.task_id,
                "provider": config.provider,
                "model": config.model,
                "max_steps": config.max_steps,
                "timeout_seconds": config.timeout_seconds,
                "prompt_version": config.prompt_version,
            },
        )
        recorder.record(TraceEventType.TASK_LOADED, payload={"task": task.model_dump(mode="json")})
        recorder.record(
            TraceEventType.STATE_SNAPSHOT,
            payload={"phase": "initial", "state": initial_state},
        )

        tools = self.environment.tool_specs()
        transcript = build_initial_transcript(task, tools)
        last_prompt_len = 0

        status = RunStatus.ERROR
        termination = TerminationReason.INTERNAL_ERROR
        final_output: str | None = None
        steps_taken = 0
        error_message: str | None = None
        current_step: int | None = None  # so a crash's error event keeps its step id

        try:
            for step_id in range(1, config.max_steps + 1):
                current_step = step_id
                if time.monotonic() - started_clock > config.timeout_seconds:
                    status = RunStatus.TERMINATED
                    termination = TerminationReason.TIMEOUT
                    break

                # Record what the model is about to see (delta since last step,
                # so traces stay readable instead of repeating the transcript).
                recorder.record(
                    TraceEventType.MODEL_PROMPT,
                    step_id=step_id,
                    payload={
                        "transcript_length": len(transcript),
                        "new_messages": [
                            m.model_dump(mode="json") for m in transcript[last_prompt_len:]
                        ],
                    },
                )
                last_prompt_len = len(transcript)

                try:
                    action = self.model_adapter.next_action(transcript, tools)
                except ScriptExhaustedError as exc:
                    recorder.record(
                        TraceEventType.ERROR,
                        step_id=step_id,
                        payload={"error": str(exc), "kind": "script_exhausted"},
                    )
                    status = RunStatus.TERMINATED
                    termination = TerminationReason.SCRIPT_EXHAUSTED
                    error_message = str(exc)
                    break
                except ModelAdapterError as exc:
                    recorder.record(
                        TraceEventType.ERROR,
                        step_id=step_id,
                        payload={"error": str(exc), "kind": "model_error"},
                    )
                    status = RunStatus.ERROR
                    termination = TerminationReason.MODEL_ERROR
                    error_message = str(exc)
                    break

                steps_taken = step_id
                recorder.record(
                    TraceEventType.MODEL_ACTION,
                    step_id=step_id,
                    payload=action.model_dump(mode="json", exclude={"raw"}),
                )
                transcript.append(_action_to_assistant_message(action))

                if action.kind is ActionKind.FINAL_ANSWER:
                    assert action.final_answer is not None
                    recorder.record(
                        TraceEventType.FINAL_ANSWER,
                        step_id=step_id,
                        payload={"final_answer": action.final_answer},
                    )
                    final_output = action.final_answer
                    status = RunStatus.COMPLETED
                    termination = TerminationReason.FINAL_ANSWER
                    break

                # Tool-call path.
                call = action.tool_call
                assert call is not None
                recorder.record(
                    TraceEventType.TOOL_CALL_REQUESTED,
                    step_id=step_id,
                    payload={"tool_name": call.tool_name, "arguments": call.arguments},
                )
                valid, validation_error = self.environment.validate_call(call)
                recorder.record(
                    TraceEventType.TOOL_CALL_VALIDATED,
                    step_id=step_id,
                    payload={
                        "tool_name": call.tool_name,
                        "valid": valid,
                        "error": validation_error,
                    },
                )

                if valid:
                    observation = self.environment.execute(call, step_id=step_id)
                    side_effect = self.environment.side_effect_for(call.tool_name)
                    recorder.record(
                        TraceEventType.TOOL_CALL_EXECUTED,
                        step_id=step_id,
                        payload={
                            "tool_name": call.tool_name,
                            "arguments": call.arguments,
                            "status": observation.status,
                            "side_effect": side_effect.value if side_effect else None,
                            "error": observation.error,
                        },
                    )
                    if observation.retrieval is not None:
                        recorder.record(
                            TraceEventType.RETRIEVAL_RESULT,
                            step_id=step_id,
                            payload={
                                "query": call.arguments.get("query"),
                                "result_count": len(observation.retrieval),
                                "results": [
                                    chunk.model_dump(mode="json", exclude={"content"})
                                    for chunk in observation.retrieval
                                ],
                            },
                        )
                else:
                    # Invalid calls are never executed; the agent observes the
                    # validation error and may recover on its next step.
                    observation = ToolResult(
                        tool_name=call.tool_name, status="error", error=validation_error
                    )

                recorder.record(
                    TraceEventType.TOOL_OBSERVATION,
                    step_id=step_id,
                    payload={
                        "tool_name": observation.tool_name,
                        "status": observation.status,
                        "result": observation.result,
                        "error": observation.error,
                    },
                )
                transcript.append(_observation_to_tool_message(observation))
            else:
                status = RunStatus.TERMINATED
                termination = TerminationReason.MAX_STEPS_REACHED
        except Exception as exc:  # noqa: BLE001 — partial artifacts beat a lost run
            recorder.record(
                TraceEventType.ERROR,
                step_id=current_step,
                payload={
                    "error": str(exc),
                    "kind": "internal_error",
                    "traceback": traceback.format_exc(),
                },
            )
            status = RunStatus.ERROR
            termination = TerminationReason.INTERNAL_ERROR
            error_message = str(exc)

        # Final snapshots and result — written even after errors, and guarded
        # so that a failing snapshot or trace write can't abort the run before
        # run_result.json lands (the partial-artifacts promise). Only the
        # run_result write itself is allowed to raise: if that fails, the disk
        # is gone and there is nothing left to preserve.
        try:
            final_state = self.environment.snapshot_state()
        except Exception as exc:  # noqa: BLE001
            logger.exception("final state snapshot failed for %s", run_id)
            final_state = {"snapshot_error": str(exc)}
            status = RunStatus.ERROR
            termination = TerminationReason.INTERNAL_ERROR
            error_message = error_message or f"final state snapshot failed: {exc}"
        try:
            recorder.record(
                TraceEventType.STATE_SNAPSHOT,
                payload={"phase": "final", "state": final_state},
            )
            recorder.record(
                TraceEventType.RUN_FINISHED,
                payload={
                    "status": status.value,
                    "termination_reason": termination.value,
                    "steps_taken": steps_taken,
                },
            )
            store.write_json(run_id, names.FINAL_STATE, final_state)
        except Exception:  # noqa: BLE001
            logger.exception("final bookkeeping failed for %s; writing run_result anyway", run_id)

        result = RunResult(
            run_id=run_id,
            task_id=task.task_id,
            status=status,
            termination_reason=termination,
            steps_taken=steps_taken,
            final_output=final_output,
            artifact_paths={
                name: f"{run_id}/{name}"
                for name in (
                    names.TASK_SPEC,
                    names.RUN_CONFIG,
                    names.INITIAL_STATE,
                    names.TRACE,
                    names.FINAL_STATE,
                    names.RUN_RESULT,
                )
            },
            started_at=started_at,
            finished_at=utc_now(),
            error=error_message,
        )
        store.write_json(run_id, names.RUN_RESULT, result)
        return result
