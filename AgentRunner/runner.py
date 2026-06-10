"""Config, the fixture model + mock tool, and the agent loop.

The "agent" is just ``run()``: ask the model for an action,  invoke a tool,
record every step to the trace, and stop when the model is done or a limit is
hit.
"""

from __future__ import annotations

import copy
import time
import uuid
from dataclasses import asdict, dataclass

from .environment import TOOLS
from .taskspec import TaskSpec
from .trace import ListTrace, event


@dataclass
class RunConfig:
    """Per-run knobs, including model/provider."""

    task_id: str
    provider: str = "fixture"
    model: str = "none"
    max_steps: int = 8
    timeout: float = 60.0

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class RunResult:
    run_id: str
    task_id: str
    termination_reason: str
    final_output: str
    steps_taken: int
    config: dict
    error: str | None = None


class FixtureModel:
    """ offline testing model:runs through script.

    Ignores the transcript; returns the next scripted action each call, or a
    ``done`` action once the script is exhausted. A real model would instead
    translate its own output into the same action shape.
    """

    def __init__(self, script: list[dict]):
        self._script = list(script)
        self._i = 0

    def next(self, transcript: list[dict]) -> dict:
        if self._i < len(self._script):
            action = self._script[self._i]
            self._i += 1
            return action
        return {"type": "done", "message": ""}



def run(task: TaskSpec, config: RunConfig, model=None, trace=None) -> RunResult:
    """Run the agent loop for a single task.

    ``model`` is anything with ``.next(transcript) -> action``; ``trace`` is
    anything with ``.emit(event)``. Both default to the offline fakes.
    """
    trace = trace or ListTrace()  # wired first so even setup failures are recorded

    run_id = str(uuid.uuid4())
    state = copy.deepcopy(task.initial_state)  # isolated world; the tool mutates it
    transcript: list[dict] = [{"role": "user", "content": task.goal}]

    final_output = ""
    steps_taken = 0
    termination_reason = "max_steps"  # default if the loop simply exhausts
    error_text: str | None = None

    trace.emit(event(run_id, 0, "run_started", {"goal": task.goal, "config": config.as_dict()}))

    start = time.time()
    try:
        # Build the default model here so a bad provider is captured, not crashed.
        if model is None:
            if config.provider != "fixture":
                raise ValueError(
                    f"Unsupported provider {config.provider!r}; only 'fixture' is wired up."
                )
            model = FixtureModel(task.script)

        for step in range(1, config.max_steps + 1):
            steps_taken = step

            if time.time() - start > config.timeout:
                termination_reason = "timeout"
                break

            action = model.next(transcript)
            trace.emit(event(run_id, step, "model_action", {"action": action}))

            kind = action.get("type")
            if kind == "done":
                final_output = action.get("message", "")
                termination_reason = "completed"
                break

            if kind == "tool":
                name, tool_args = action.get("name"), action.get("args", {})
                trace.emit(event(run_id, step, "tool_call", {"name": name, "args": tool_args}))
                if name not in task.allowed_tools:
                    result = {"ok": False, "error": f"tool not allowed: {name!r}"}
                elif name not in TOOLS:
                    result = {"ok": False, "error": f"tool not registered: {name!r}"}
                else:
                    result = TOOLS[name](tool_args, state)
                trace.emit(event(run_id, step, "tool_result", {"name": name, "result": result}))
                transcript.append({"role": "tool", "content": result})
                continue

            # "say" (or anything else): record the message and keep going.
            final_output = action.get("message", "")
            transcript.append({"role": "assistant", "content": final_output})

    except Exception as exc:  # errors become a termination reason, never a crash
        termination_reason = "error"
        error_text = f"{type(exc).__name__}: {exc}"
        trace.emit(
            event(run_id, steps_taken, "error", {"type": type(exc).__name__, "message": str(exc)})
        )

    # Single exit path: emit run_finished and build the result exactly once.
    trace.emit(
        event(
            run_id,
            steps_taken,
            "run_finished",
            {"termination_reason": termination_reason, "final_output": final_output},
        )
    )
    return RunResult(
        run_id=run_id,
        task_id=task.task_id,
        termination_reason=termination_reason,
        final_output=final_output,
        steps_taken=steps_taken,
        config=config.as_dict(),
        error=error_text,
    )
