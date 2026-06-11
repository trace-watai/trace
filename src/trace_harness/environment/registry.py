"""ToolRegistry: the catalog an environment draws its tools from.

Why a registry instead of a global TOOLS dict
    The runner must never import tools directly — the environment owns
    them. A registry instance makes the available toolset explicit,
    swappable in tests (e.g. a registry with a broken tool), and filterable
    per task (``TaskSpec.available_tools``).

# TODO(Evan Yang/environment): when a second workflow lands, decide whether
# registries are assembled per workflow_type (likely) and add a lookup from
# workflow_type -> registry factory here.
"""

from __future__ import annotations

from trace_harness.environment.tools import ToolDefinition, support_tool_definitions


class ToolRegistry:
    """A name -> ToolDefinition catalog with explicit registration."""

    def __init__(self, definitions: list[ToolDefinition] | None = None):
        self._tools: dict[str, ToolDefinition] = {}
        for definition in definitions or []:
            self.register(definition)

    def register(self, definition: ToolDefinition) -> None:
        if definition.name in self._tools:
            raise ValueError(f"tool '{definition.name}' is already registered")
        self._tools[definition.name] = definition

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def definitions(self) -> list[ToolDefinition]:
        return [self._tools[name] for name in self.names()]


def default_support_registry() -> ToolRegistry:
    """The standard registry for the support/refund vertical slice."""
    return ToolRegistry(support_tool_definitions())
