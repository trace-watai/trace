"""Shared agent-facing contract types and the ModelAdapter protocol.

These are the types every adapter speaks, regardless of provider:

- :class:`Message` — one entry in the running transcript.
- :class:`ToolSpec` — a provider-neutral tool description (name, description,
  JSON-schema parameters) derived from the environment's tool definitions.
- :class:`AgentAction` — the *normalized* output of one model turn: either a
  tool call or a final answer, optionally with reasoning text.

Keeping these in one leaf module lets the environment, runner, and adapters
all import them without circular dependencies.

Intended evolution
    Real providers return richer payloads (token usage, safety metadata,
    parallel tool calls). ``AgentAction.raw`` is the escape hatch for that
    today; if/when we support parallel tool calls, ``AgentAction`` grows a
    list form behind a schema version bump — do not bolt it on silently.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field, model_validator


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class Message(BaseModel):
    """One transcript entry, in provider-neutral form."""

    role: MessageRole
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolSpec(BaseModel):
    """What a model is told about a tool. Parameters are a JSON schema dict."""

    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict)


class ToolCall(BaseModel):
    """A request from the model to invoke one tool with JSON arguments."""

    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ActionKind(StrEnum):
    TOOL_CALL = "tool_call"
    FINAL_ANSWER = "final_answer"


class AgentAction(BaseModel):
    """Normalized output of one model turn.

    Exactly one of ``tool_call`` / ``final_answer`` is meaningful, selected
    by ``kind``. ``reasoning`` is optional free text: fixture scripts use it
    to make failure anatomy visible to attribution; real adapters may or may
    not be able to populate it (attribution must degrade gracefully).
    """

    kind: ActionKind
    tool_call: ToolCall | None = None
    final_answer: str | None = None
    reasoning: str | None = None
    # Provider-specific raw response payload, for debugging real adapters.
    raw: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _payload_matches_kind(self) -> AgentAction:
        if self.kind is ActionKind.TOOL_CALL and self.tool_call is None:
            raise ValueError("AgentAction(kind=tool_call) requires tool_call")
        if self.kind is ActionKind.FINAL_ANSWER and self.final_answer is None:
            raise ValueError("AgentAction(kind=final_answer) requires final_answer")
        return self


class ModelAdapterError(RuntimeError):
    """Base class for adapter failures the runner should treat as model errors."""


class ScriptExhaustedError(ModelAdapterError):
    """A fixture script ran out of actions before the run terminated."""


@runtime_checkable
class ModelAdapter(Protocol):
    """The single interface the runner uses to get the agent's next move.

    Implementations must be stateless across runs *or* explicitly
    single-run (the fixture adapter is single-run: it holds a cursor).
    """

    name: str

    def next_action(self, transcript: list[Message], tools: list[ToolSpec]) -> AgentAction:
        """Return the next action given the transcript so far and available tools."""
        ...
