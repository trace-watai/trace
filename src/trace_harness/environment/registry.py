"""ToolRegistry: the catalog an environment draws its tools from.

Why a registry instead of a global TOOLS dict
    The runner must never import tools directly — the environment owns
    them. A registry instance makes the available toolset explicit,
    swappable in tests (e.g. a registry with a broken tool), and filterable
    per task (``TaskSpec.available_tools``).

Workflow selection lives in ``factory.py``; registries hold only tool definitions.
"""

from __future__ import annotations

from typing import Generic, TypeVar

from trace_harness.environment.state import SupportState
from trace_harness.environment.tools import ToolDefinition, support_tool_definitions

StateT = TypeVar("StateT")


class ToolRegistry(Generic[StateT]):
    """A name -> ToolDefinition catalog with explicit registration."""

    def __init__(self, definitions: list[ToolDefinition[StateT]] | None = None):
        self._tools: dict[str, ToolDefinition[StateT]] = {}
        for definition in definitions or []:
            self.register(definition)

    def register(self, definition: ToolDefinition[StateT]) -> None:
        if definition.name in self._tools:
            raise ValueError(f"tool '{definition.name}' is already registered")
        self._tools[definition.name] = definition

    def get(self, name: str) -> ToolDefinition[StateT] | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def definitions(self) -> list[ToolDefinition[StateT]]:
        return [self._tools[name] for name in self.names()]


def default_support_registry() -> ToolRegistry[SupportState]:
    """The standard registry for the support/refund vertical slice."""
    return ToolRegistry(support_tool_definitions())
