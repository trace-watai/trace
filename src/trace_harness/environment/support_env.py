"""Support/refund state and tools on the shared sandbox execution seams."""

from __future__ import annotations

from pathlib import Path

from trace_harness.environment.registry import ToolRegistry, default_support_registry
from trace_harness.environment.sandbox import SandboxEnvironment
from trace_harness.environment.state import Doc, SupportState
from trace_harness.tasks.schemas import TaskSpec


class SupportEnvironment(SandboxEnvironment[SupportState]):
    workflow_type = "support.refund"

    def __init__(
        self,
        state: SupportState,
        registry: ToolRegistry[SupportState] | None = None,
        available_tools: list[str] | None = None,
        control_library: Path | str | None = None,
        task: TaskSpec | None = None,
    ):
        super().__init__(
            state,
            registry or default_support_registry(),
            available_tools,
            control_library,
            task,
        )

    @classmethod
    def from_task(
        cls,
        task: TaskSpec,
        docs: list[Doc] | None = None,
        registry: ToolRegistry[SupportState] | None = None,
        control_library: Path | str | None = None,
    ) -> SupportEnvironment:
        return cls(
            SupportState.from_task(task, docs=docs),
            registry=registry,
            available_tools=task.available_tools,
            control_library=control_library,
            task=task,
        )
