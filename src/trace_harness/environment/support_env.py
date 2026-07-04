"""SupportEnvironment: the sandboxed support/refund world, wired for the runner.

This class is what the runner sees (structurally, via the ``ToolEnvironment``
protocol in ``runner/agent_runner.py``): tool specs, call validation, call
execution, and state snapshots. It owns the mutable :class:`SupportState`
and the per-task tool subset; the runner stays generic and scenario-free.

Validation vs execution
    ``validate_call`` answers "would this call be accepted?" without side
    effects, so the runner can record a ``tool_call_validated`` trace event
    before anything runs. ``execute`` re-validates internally (single source
    of truth) and then dispatches — an invalid call can therefore never
    execute, even if a caller skips ``validate_call``.

Pre-execute hooks
    ``register_pre_execute_hook`` attaches a guardrail that runs after
    argument validation but before the tool handler fires. A hook receives
    the call and the live state; returning a :class:`ToolResult` short-
    circuits dispatch (the handler never runs, so no state mutation occurs).
    Returning ``None`` passes control to the next hook or the handler.
    Hooks run in registration order; the first non-``None`` return wins.

    This is the seam named in the repair package for the refund failure
    scenario. The MVP intentionally ships with no hooks registered so the
    verifier catches real violations; a guardrail is installed by callers
    after construction, not hardcoded here.

Edge cases for verifier inspection
    - Two distinct error paths exist: (1) validation failure — the tool is
      never executed and no ``TOOL_CALL_EXECUTED`` event is emitted;
      (2) handler error (e.g. customer not found) — execution ran and
      ``TOOL_CALL_EXECUTED`` is emitted with ``status="error"``. The
      verifier must distinguish these from the trace.
    - State mutations have no rollback. If a handler mutates state and then
      raises, the mutation persists in the final snapshot. A run with
      ``RunStatus=ERROR`` does not imply a clean ``final_state``.
    - ``step_id`` is always non-``None`` when called from the runner, but
      the field is nullable on ``Refund`` and ``Ticket``. The verifier must
      not assume ``issued_at_step`` is set on arbitrary ``SupportState``
      instances constructed outside a run.
    - ``snapshot()`` returns a deep copy via ``model_dump(mode="json")``.
      Mutating the returned dict does not affect live state.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from trace_harness.environment.registry import ToolRegistry, default_support_registry
from trace_harness.environment.state import Doc, SupportState
from trace_harness.environment.tools import ToolResult, ToolSideEffect
from trace_harness.models.base import ToolCall, ToolSpec
from trace_harness.tasks.schemas import TaskSpec


class SupportEnvironment:
    """Tools + mutable state for one run of a support/refund task."""

    def __init__(
        self,
        state: SupportState,
        registry: ToolRegistry | None = None,
        available_tools: list[str] | None = None,
    ):
        self.state = state
        self._registry = registry or default_support_registry()
        self._pre_execute_hooks: list[Callable[[ToolCall, SupportState], ToolResult | None]] = []
        if available_tools is None:
            self._available = self._registry.names()
        else:
            unknown = sorted(set(available_tools) - set(self._registry.names()))
            if unknown:
                raise ValueError(
                    f"task requests tools not in the registry: {unknown}; "
                    f"registry has {self._registry.names()}"
                )
            self._available = list(available_tools)

    @classmethod
    def from_task(
        cls,
        task: TaskSpec,
        docs: list[Doc] | None = None,
        registry: ToolRegistry | None = None,
    ) -> SupportEnvironment:
        """Build the environment a task describes: initial state + tool subset."""
        state = SupportState.from_task(task, docs=docs)
        return cls(state, registry=registry, available_tools=task.available_tools)

    def register_pre_execute_hook(
        self, hook: Callable[[ToolCall, SupportState], ToolResult | None]
    ) -> None:
        """Attach a guardrail that runs before every tool dispatch.

        The hook receives the validated call and live state. Return a
        ``ToolResult`` to block execution; return ``None`` to pass through.
        Hooks run in registration order; first non-``None`` return wins.
        """
        self._pre_execute_hooks.append(hook)

    # --- ToolEnvironment protocol ---

    def tool_specs(self) -> list[ToolSpec]:
        specs = []
        for name in self._available:
            definition = self._registry.get(name)
            assert definition is not None  # guaranteed by constructor check
            specs.append(definition.spec())
        return specs

    def validate_call(self, call: ToolCall) -> tuple[bool, str | None]:
        """Check tool existence and argument shape without executing anything."""
        if call.tool_name not in self._available:
            return False, (f"unknown tool '{call.tool_name}'; available tools: {self._available}")
        definition = self._registry.get(call.tool_name)
        assert definition is not None
        try:
            definition.args_model.model_validate(call.arguments)
        except ValidationError as exc:
            return False, f"invalid arguments for '{call.tool_name}': {exc.errors()}"
        return True, None

    def execute(self, call: ToolCall, step_id: int | None = None) -> ToolResult:
        """Validate and run one tool call, mutating state on success.

        Tool-level failures (unknown customer, bad filter) come back as
        ``status="error"`` results — they are observations the agent sees,
        not harness crashes.
        """
        ok, error = self.validate_call(call)
        if not ok:
            return ToolResult(tool_name=call.tool_name, status="error", error=error)
        definition = self._registry.get(call.tool_name)
        assert definition is not None
        args = definition.args_model.model_validate(call.arguments)
        for hook in self._pre_execute_hooks:
            early = hook(call, self.state)
            if early is not None:
                return early
        return definition.handler(self.state, args, step_id)

    def side_effect_for(self, tool_name: str) -> ToolSideEffect | None:
        definition = self._registry.get(tool_name)
        return definition.side_effect if definition else None

    def snapshot_state(self) -> dict[str, Any]:
        return self.state.snapshot()
