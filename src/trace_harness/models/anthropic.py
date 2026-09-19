"""AnthropicModelAdapter, the second real provider (#160).

Why this exists
    Every live result TRACE could produce came through one vendor. The
    experiment plan in #158 compares two model families and the "different
    model" condition in #159 cannot run with one, so a single credential was
    blocking the research, not just adding risk. The live acceptance run in
    #125 waited six weeks on that one key and then failed on its second turn.

Design, the same as models/gemini.py
    The three conversion helpers are pure and SDK-free, so they unit-test
    offline with no ``anthropic`` install, no key and no network. Only
    ``next_action`` touches the live SDK, and that path is verified by running
    a task with ``--provider anthropic`` rather than in the suite. Tests are
    offline forever.

Where Anthropic differs from Gemini, and what that costs
    Tool results are paired by id. A ``tool_use`` block carries an ``id`` and
    the ``tool_result`` that answers it has to quote that id, where Gemini
    pairs by function name. The id rides in ``AgentAction.provider_state``
    exactly as Gemini's thought signature does, so the runner needs no
    provider-specific knowledge.

    There is no seed parameter. A seed reaching this adapter is recorded in
    ``run_config.json`` and never sent, because pretending a run was seeded
    when the provider ignored it would make an unreproducible run look
    reproducible. Runs seeded against Gemini and against Anthropic are not
    comparable on that axis and the trace has to say so.

    ``max_tokens`` is required by the API rather than optional. The default
    below is large enough for a refund-task turn and is a constructor knob.

Out of scope, the same as the Gemini adapter: retries, backoff, rate limiting,
parallel tool calls, streaming.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from trace_harness.models.base import (
    ActionKind,
    AgentAction,
    Message,
    MessageRole,
    ModelAdapterError,
    ProviderNotConfiguredError,
    ToolCall,
    ToolSpec,
)

if TYPE_CHECKING:  # typing only, the runtime import stays lazy inside methods
    import anthropic

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5"

#: Anthropic requires max_tokens. A refund-task turn is a short tool call or a
#: few sentences, so this is generous rather than tuned.
DEFAULT_MAX_TOKENS = 4096

#: Key under AgentAction.provider_state / Message.metadata["provider_state"]
#: holding the id of the tool_use block this turn produced. The next turn's
#: tool_result has to quote it or the API rejects the request.
TOOL_USE_ID_KEY = "tool_use_id"

#: USD per million tokens, keyed by model name. Kept as data so a price change
#: is a one-line diff and an unpriced model is visibly absent rather than
#: silently costed at zero.
ANTHROPIC_PRICING: dict[str, tuple[float, float]] = {
    # model: (input per million, output per million)
    "claude-opus-5": (15.0, 75.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
}


class AnthropicNotConfiguredError(ProviderNotConfiguredError):
    """Raised at construction time when the Anthropic adapter cannot be used."""


AnthropicMessage = dict[str, Any]
ToolDefinition = dict[str, Any]


def _transcript_to_messages(
    transcript: list[Message],
) -> tuple[str | None, list[AnthropicMessage]]:
    """Convert the runner's transcript into (system, messages).

    Anthropic takes the system prompt as its own parameter rather than as a
    message role, so SYSTEM messages collect into the returned string and
    everything else becomes a message.

        USER      -> {"role": "user", "content": [{"type": "text", ...}]}
        ASSISTANT -> a tool_use block when metadata carries a tool call, with
                     the id recorded in provider_state, otherwise a text block
        TOOL      -> {"role": "user", "content": [{"type": "tool_result",
                      "tool_use_id": <the id from the assistant turn>, ...}]}

    A tool result has to quote the id of the tool_use it answers. That id
    arrives on the assistant turn before it, so the mapping carries the most
    recent one forward. A tool result with no preceding tool_use would be
    rejected by the API, and an empty id here is what makes that visible at the
    boundary rather than as a 400 halfway through a run.
    """
    system_parts: list[str] = []
    messages: list[AnthropicMessage] = []
    pending_tool_use_id: str | None = None

    for msg in transcript:
        if msg.role is MessageRole.SYSTEM:
            if msg.content:
                system_parts.append(msg.content)
        elif msg.role is MessageRole.USER:
            messages.append({"role": "user", "content": [{"type": "text", "text": msg.content}]})
        elif msg.role is MessageRole.ASSISTANT:
            tool_call = msg.metadata.get("tool_call")
            if tool_call:
                pending_tool_use_id = (msg.metadata.get("provider_state") or {}).get(
                    TOOL_USE_ID_KEY
                ) or ""
                messages.append(
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": pending_tool_use_id,
                                "name": tool_call["tool_name"],
                                "input": tool_call.get("arguments", {}),
                            }
                        ],
                    }
                )
            else:
                messages.append(
                    {"role": "assistant", "content": [{"type": "text", "text": msg.content}]}
                )
        elif msg.role is MessageRole.TOOL:
            md = msg.metadata
            error = md.get("error")
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": pending_tool_use_id or "",
                            "content": _tool_result_text(md.get("result"), error),
                            "is_error": bool(error),
                        }
                    ],
                }
            )
            pending_tool_use_id = None
    system = "\n".join(system_parts) if system_parts else None
    return system, messages


def _tool_result_text(result: Any, error: str | None) -> str:
    """Render a tool result as the text block Anthropic expects.

    An error is sent as the content with ``is_error`` set rather than dropped,
    because the agent recovering from a tool failure is behavior the verifier
    is entitled to judge.
    """
    if error:
        return str(error)
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    import json

    return json.dumps(result, sort_keys=True, default=str)


def _tools_to_definitions(tools: list[ToolSpec]) -> list[ToolDefinition]:
    """Convert ToolSpecs into Anthropic tool definitions.

    ``ToolSpec.parameters`` is already a JSON schema, so it drops straight into
    ``input_schema``. A tool with no parameters still needs an object schema,
    since the API rejects a bare empty schema.
    """
    return [
        {
            "name": t.name,
            "description": t.description,
            "input_schema": t.parameters or {"type": "object", "properties": {}},
        }
        for t in tools
    ]


def _normalize_response(response: Any) -> AgentAction:
    """Normalize an Anthropic response into exactly one AgentAction.

    Duck-typed on ``response.content`` and ``response.stop_reason`` so a fake
    object stands in for the SDK's.

    More than one tool_use block is an error rather than a silent drop. TRACE
    attributes a failure to a step, and two actions recorded as one step would
    make that attribution point at something that never happened.
    """
    raw = _response_to_dict(response)
    blocks = list(getattr(response, "content", None) or [])
    tool_uses = [b for b in blocks if getattr(b, "type", None) == "tool_use"]

    if len(tool_uses) > 1:
        raise ModelAdapterError(
            "Anthropic returned multiple tool calls, but TRACE requires exactly one action "
            "per turn; parallel tool calls are not supported"
        )
    if getattr(response, "stop_reason", None) == "refusal":
        raise ModelAdapterError("Anthropic declined to respond (stop_reason 'refusal')")

    text = _text_from_blocks(blocks)
    if tool_uses:
        call = tool_uses[0]
        tool_use_id = getattr(call, "id", None)
        return AgentAction(
            kind=ActionKind.TOOL_CALL,
            tool_call=ToolCall(
                tool_name=getattr(call, "name", ""),
                arguments=dict(getattr(call, "input", None) or {}),
            ),
            reasoning=text,
            raw=raw,
            provider_state={TOOL_USE_ID_KEY: tool_use_id} if tool_use_id else None,
        )
    if text:
        return AgentAction(kind=ActionKind.FINAL_ANSWER, final_answer=text, raw=raw)
    raise ModelAdapterError(
        "Anthropic returned neither a tool call nor text (empty or blocked response)"
    )


def _text_from_blocks(blocks: list[Any]) -> str | None:
    """Join every text block, so text alongside a tool call is kept as reasoning."""
    text = "\n".join(
        block_text
        for block in blocks
        if getattr(block, "type", None) == "text" and (block_text := getattr(block, "text", None))
    )
    return text or None


def _response_to_dict(response: Any) -> dict[str, Any]:
    """Best-effort JSON-able dict of the raw provider response for the trace."""
    dump = getattr(response, "model_dump", None)
    if callable(dump):
        try:
            return dump(mode="json")
        except Exception:  # noqa: BLE001 - fall through to looser strategies
            try:
                return dump()
            except Exception:  # noqa: BLE001
                pass
    try:
        return dict(response)
    except Exception:  # noqa: BLE001
        return {}


def extract_usage(raw: dict[str, Any]) -> tuple[int, int] | None:
    """Read (input_tokens, output_tokens) out of a recorded raw response.

    Returns None when the response carries no usage, which is what a fixture or
    a cassette replay looks like. None and ``(0, 0)`` mean different things, so
    an absent usage block never becomes a zero cost.
    """
    usage = raw.get("usage")
    if not isinstance(usage, dict):
        return None
    got_in = usage.get("input_tokens")
    got_out = usage.get("output_tokens")
    if not isinstance(got_in, int) or not isinstance(got_out, int):
        return None
    return got_in, got_out


def estimate_cost_usd(model: str, raws: list[dict[str, Any]]) -> float | None:
    """Price every recorded response for ``model``, or None if it cannot be priced.

    An unpriced model returns None rather than 0.0. A run that cost money and
    reports zero is worse than one that reports nothing, because only the
    second is visibly missing.
    """
    price = ANTHROPIC_PRICING.get(model)
    if price is None:
        return None
    per_input, per_output = price
    usages = [usage for raw in raws if (usage := extract_usage(raw)) is not None]
    if not usages:
        return None
    total = sum(
        (got_in * per_input + got_out * per_output) / 1_000_000 for got_in, got_out in usages
    )
    return round(total, 6)


class AnthropicModelAdapter:
    """Adapter for Anthropic Claude models.

    Construction validates configuration, so ``--provider anthropic`` fails
    fast with instructions. ``next_action`` uses the pure helpers above and
    keeps the optional SDK import at the live-call boundary.
    """

    name = "anthropic"

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        *,
        temperature: float | None = None,
        seed: int | None = None,
        timeout_seconds: float = 120.0,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ):
        self.model = model or DEFAULT_ANTHROPIC_MODEL
        self.temperature = temperature
        # Recorded so run_config.json keeps what was asked for, never sent,
        # because the Messages API has no seed parameter.
        self.seed = seed
        self.timeout_seconds = timeout_seconds
        self.max_tokens = max_tokens
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not self.api_key:
            raise AnthropicNotConfiguredError(
                "ANTHROPIC_API_KEY is not set. Get a key from "
                "https://console.anthropic.com/settings/keys, put it in your "
                "local .env (see .env.example), and re-run. The fixture "
                "provider (TRACE_MODEL_PROVIDER=fixture) needs no key and is "
                "the default for all tests and CI."
            )
        self._client_obj: anthropic.Anthropic | None = None

    def _client(self) -> anthropic.Anthropic:
        """Lazily build the SDK client, so a non-Anthropic run never needs the
        ``anthropic`` package installed."""
        if self._client_obj is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - optional dependency
                raise AnthropicNotConfiguredError(
                    "the 'anthropic' package is not installed; "
                    'install it with: pip install -e ".[anthropic]"'
                ) from exc
            self._client_obj = anthropic.Anthropic(
                api_key=self.api_key,
                # Seconds here, unlike Gemini's milliseconds. Complements the
                # runner's between-call timeout rather than replacing it.
                timeout=self.timeout_seconds,
            )
        return self._client_obj

    def next_action(self, transcript: list[Message], tools: list[ToolSpec]) -> AgentAction:
        client = self._client()
        system, messages = _transcript_to_messages(transcript)
        definitions = _tools_to_definitions(tools)

        import anthropic

        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": messages,
        }
        if system is not None:
            request["system"] = system
        if definitions:
            request["tools"] = definitions
        if self.temperature is not None:
            request["temperature"] = self.temperature

        try:
            response = client.messages.create(**request)
        except anthropic.APIError as exc:
            # Map provider errors to the runner's clean model_error termination.
            raise ModelAdapterError(f"Anthropic API call failed: {exc}") from exc
        return _normalize_response(response)
