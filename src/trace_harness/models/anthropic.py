"""AnthropicModelAdapter, the second real provider (#160).

Why this exists
    Every live result TRACE could produce came through one vendor. The
    experiment plan in #158 compares two model families and the "different
    model" condition in #159 cannot run with one, so a single credential
    blocked the research outright. The live acceptance run in #125 waited six
    weeks on that one key and then failed on its second turn.

Design, the same as models/gemini.py
    The conversion helpers are pure and SDK-free, so they unit-test offline
    with no ``anthropic`` install, no key and no network. Only the constructor
    and ``next_action`` touch the SDK, and the tests stand a fake module in for
    it, so the request this adapter builds is checked offline too. The live
    path itself is verified by running a task with ``--provider anthropic``.

Where Anthropic differs from Gemini, and what that costs
    Tool results are paired by id. A ``tool_use`` block carries an ``id`` and
    the ``tool_result`` that answers it has to quote that id, where Gemini
    pairs by function name. The id rides in ``AgentAction.provider_state``
    exactly as Gemini's thought signature does, so the runner needs no
    provider-specific knowledge. A turn with no id of its own, such as a
    scripted prefix replayed before a live continuation, gets a stable id made
    from its position, so its result still pairs.

    Sonnet 5 and the later Opus and Fable models think by default, and
    Anthropic's thinking docs require a tool-use turn to go back with its
    ``thinking`` and ``redacted_thinking`` blocks, unmodified and in front of
    the ``tool_use``. An edited or partial set is rejected with a 400, and
    dropping them all loses the reasoning that led to the call. Those blocks
    ride in ``provider_state`` as well
    (https://platform.claude.com/docs/en/build-with-claude/thinking).

    Those models, and Opus 4.7 and 4.8 as well, return 400 for a non-default
    ``temperature``, ``top_p`` or ``top_k``. A temperature configured for one
    of them is refused when the adapter is built, before any run exists,
    because sending it would fail every call and dropping it would record a
    setting the run never had. For models that accept it, it goes in
    ``extra_body``, since anthropic 1.x removed the sampling parameters from
    ``messages.create``.

    There is no seed parameter. A seed reaching this adapter is recorded in
    ``run_config.json``, marked ``seed_sent: false`` in its metadata, and never
    sent, because pretending a run was seeded when the provider ignored it
    would make an unreproducible run look reproducible.

    ``max_tokens`` is required by the API, and thinking counts toward it. A
    turn that hits it, or the context window, is truncated and becomes a model
    error with the billed response kept in the trace.

Out of scope, the same as the Gemini adapter: retries, backoff, rate limiting,
streaming. Parallel tool calls are switched off on the request with
``disable_parallel_tool_use``, and a response carrying two anyway is an error.
"""

from __future__ import annotations

import json
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

if TYPE_CHECKING:  # typing only, the runtime import happens in the constructor
    import anthropic

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5"

#: Anthropic requires max_tokens, and on models that think by default the
#: thinking counts toward it, so a budget sized for a short tool call can be
#: spent before the call is written. 16000 is the non-streaming size
#: Anthropic's own guidance uses; the SDK insists on streaming above 21,333.
#: Only tokens actually generated are billed, so a larger ceiling costs nothing
#: by itself.
DEFAULT_MAX_TOKENS = 16000

#: Key under AgentAction.provider_state / Message.metadata["provider_state"]
#: holding the id of the tool_use block this turn produced. The next turn's
#: tool_result has to quote it or the API rejects the request.
TOOL_USE_ID_KEY = "tool_use_id"

#: Key holding the turn's thinking and redacted_thinking blocks, in the order
#: the model produced them and exactly as received. They are sent back in
#: front of the tool_use block on every later request.
THINKING_BLOCKS_KEY = "thinking_blocks"

#: USD per million tokens, keyed by model name, as (input, output). Kept as
#: data so a price change is a one-line diff and an unpriced model is visibly
#: absent rather than silently costed at zero. Source:
#: https://platform.claude.com/docs/en/about-claude/pricing, checked 2026-09-24.
#: claude-haiku-4-5 is the alias of the dated snapshot; both are billed alike.
ANTHROPIC_PRICING: dict[str, tuple[float, float]] = {
    # model: (input per million, output per million)
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
}

#: Prompt-cache tokens are billed as multiples of the input price, from the same
#: pricing page. Caching is not only opt-in: during a tool-use loop the history
#: before a tool result, thinking blocks included, is cached automatically, so
#: a run can read from the cache without ever asking to.
CACHE_READ_MULTIPLIER = 0.1
CACHE_READ_MULTIPLIER_BY_MODEL: dict[str, float] = {
    "claude-opus-5-5": 0.05,
    "claude-fable-5-1": 0.025,
}
CACHE_WRITE_5M_MULTIPLIER = 1.25
CACHE_WRITE_1H_MULTIPLIER = 2.0

#: Models that return 400 for a non-default temperature, top_p or top_k on
#: every request, whether or not thinking is on. From "Sampling parameters" on
#: https://platform.claude.com/docs/en/build-with-claude/thinking and the
#: Sonnet 5 model page, checked 2026-09-24. Older models, Haiku 4.5 among them,
#: still accept a temperature while thinking is off, which is their default.
FIXED_SAMPLING_MODELS = frozenset(
    {
        "claude-fable-5-1",
        "claude-mythos-5-1",
        "claude-fable-5",
        "claude-mythos-5",
        "claude-opus-5-5",
        "claude-opus-5",
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-sonnet-5",
    }
)

#: stop_reason values that mean the turn was cut off before it finished. What
#: came back is billed but incomplete, so it is never taken as the action.
_TRUNCATED_STOP_REASONS = frozenset({"max_tokens", "model_context_window_exceeded"})

#: The fields of each thinking block type, as the Messages API documents them.
#: Copied verbatim, because the API rejects a thinking block that changed.
_THINKING_BLOCK_FIELDS = {
    "thinking": ("type", "thinking", "signature"),
    "redacted_thinking": ("type", "data"),
}


class AnthropicNotConfiguredError(ProviderNotConfiguredError):
    """Raised at construction time when the Anthropic adapter cannot be used."""


AnthropicMessage = dict[str, Any]
ToolDefinition = dict[str, Any]


def check_sampling(model: str, temperature: float | None) -> None:
    """Refuse a temperature the model would reject with a 400 on every call."""
    if temperature is not None and model in FIXED_SAMPLING_MODELS:
        raise AnthropicNotConfiguredError(
            f"{model} rejects a non-default temperature, top_p or top_k with a 400 "
            f"on every request, so temperature={temperature} cannot be sent. Leave "
            "temperature unset for this model, or choose one that accepts it such as "
            "claude-haiku-4-5. See https://platform.claude.com/docs/en/build-with-claude/thinking"
        )


def _synthetic_tool_use_id(position: int) -> str:
    """A stable id for a tool call that arrived without one.

    A scripted prefix, or a turn another provider produced, has no Anthropic
    id. The position in the transcript never changes once written, so the id
    is the same on every later request and the tool_result that follows can
    quote it.
    """
    return f"toolu_trace_{position}"


def _transcript_to_messages(
    transcript: list[Message],
) -> tuple[str | None, list[AnthropicMessage]]:
    """Convert the runner's transcript into (system, messages).

    Anthropic takes the system prompt as its own parameter rather than as a
    message role, so SYSTEM messages collect into the returned string and
    everything else becomes a message.

        USER      -> {"role": "user", "content": [{"type": "text", ...}]}
        ASSISTANT -> when metadata carries a tool call, the turn's thinking
                     blocks from provider_state followed by a tool_use block
                     with the id recorded there; otherwise a text block
        TOOL      -> {"role": "user", "content": [{"type": "tool_result",
                      "tool_use_id": <the id from the assistant turn>, ...}]}

    A tool result has to quote the id of the tool_use it answers. That id
    arrives on the assistant turn before it, so the mapping carries the most
    recent one forward. A tool result with no tool call before it cannot be
    paired with anything, and is an adapter error here rather than a 400
    halfway through a run.
    """
    system_parts: list[str] = []
    messages: list[AnthropicMessage] = []
    pending_tool_use_id: str | None = None

    for position, msg in enumerate(transcript):
        if msg.role is MessageRole.SYSTEM:
            if msg.content:
                system_parts.append(msg.content)
        elif msg.role is MessageRole.USER:
            messages.append({"role": "user", "content": [{"type": "text", "text": msg.content}]})
        elif msg.role is MessageRole.ASSISTANT:
            tool_call = msg.metadata.get("tool_call")
            if tool_call:
                state = msg.metadata.get("provider_state") or {}
                pending_tool_use_id = state.get(TOOL_USE_ID_KEY) or _synthetic_tool_use_id(position)
                content = [dict(block) for block in state.get(THINKING_BLOCKS_KEY) or []]
                content.append(
                    {
                        "type": "tool_use",
                        "id": pending_tool_use_id,
                        "name": tool_call["tool_name"],
                        "input": tool_call.get("arguments", {}),
                    }
                )
                messages.append({"role": "assistant", "content": content})
            else:
                messages.append(
                    {"role": "assistant", "content": [{"type": "text", "text": msg.content}]}
                )
        elif msg.role is MessageRole.TOOL:
            if pending_tool_use_id is None:
                raise ModelAdapterError(
                    "a tool result has no tool call before it in the transcript, so "
                    "there is no tool_use id for it to quote"
                )
            md = msg.metadata
            error = md.get("error")
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": pending_tool_use_id,
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

    A response that cannot become one action is an error carrying the raw
    response, since the provider billed for it and the trace has to show it.
    """
    raw = _response_to_dict(response)
    try:
        return _action_from_response(response, raw)
    except ModelAdapterError as exc:
        exc.raw = raw
        raise


def _action_from_response(response: Any, raw: dict[str, Any]) -> AgentAction:
    """The one action a response holds, or a ModelAdapterError saying why not.

    More than one tool_use block is an error rather than a silent drop. TRACE
    attributes a failure to a step, and two actions recorded as one step would
    make that attribution point at something that never happened.
    """
    stop_reason = getattr(response, "stop_reason", None)
    if stop_reason in _TRUNCATED_STOP_REASONS:
        raise ModelAdapterError(
            f"Anthropic stopped the turn early (stop_reason {stop_reason!r}), so what "
            "came back is incomplete; raise max_tokens if this recurs"
        )
    if stop_reason == "refusal":
        raise ModelAdapterError("Anthropic declined to respond (stop_reason 'refusal')")

    blocks = list(getattr(response, "content", None) or [])
    tool_uses = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
    if len(tool_uses) > 1:
        raise ModelAdapterError(
            "Anthropic returned multiple tool calls, but TRACE requires exactly one action "
            "per turn; parallel tool calls are not supported"
        )

    text = _text_from_blocks(blocks)
    if tool_uses:
        call = tool_uses[0]
        tool_use_id = getattr(call, "id", None)
        if not tool_use_id:
            raise ModelAdapterError(
                "Anthropic returned a tool_use block with no id, so its result could "
                "never be paired with it"
            )
        state: dict[str, Any] = {TOOL_USE_ID_KEY: tool_use_id}
        thinking = _thinking_blocks(blocks)
        if thinking:
            state[THINKING_BLOCKS_KEY] = thinking
        return AgentAction(
            kind=ActionKind.TOOL_CALL,
            tool_call=ToolCall(
                tool_name=getattr(call, "name", ""),
                arguments=dict(getattr(call, "input", None) or {}),
            ),
            reasoning=text,
            raw=raw,
            provider_state=state,
        )
    if text:
        # A final answer ends the run, so there is no later request its
        # thinking would have to go back in.
        return AgentAction(kind=ActionKind.FINAL_ANSWER, final_answer=text, raw=raw)
    raise ModelAdapterError(
        "Anthropic returned neither a tool call nor text (empty or blocked response)"
    )


def _thinking_blocks(blocks: list[Any]) -> list[dict[str, Any]]:
    """The thinking and redacted_thinking blocks, in order, field for field.

    With the default ``display: "omitted"`` the ``thinking`` text is empty and
    the ``signature`` carries the reasoning encrypted. Both go back unchanged.
    """
    kept = []
    for block in blocks:
        fields = _THINKING_BLOCK_FIELDS.get(getattr(block, "type", None))
        if fields is not None:
            kept.append({name: getattr(block, name) for name in fields if hasattr(block, name)})
    return kept


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
    an absent usage block never becomes a zero cost. ``input_tokens`` excludes
    tokens read from or written to the prompt cache; see
    :func:`extract_cache_usage`.
    """
    usage = raw.get("usage")
    if not isinstance(usage, dict):
        return None
    got_in = usage.get("input_tokens")
    got_out = usage.get("output_tokens")
    if not isinstance(got_in, int) or not isinstance(got_out, int):
        return None
    return got_in, got_out


def extract_cache_usage(raw: dict[str, Any]) -> tuple[int, int, int]:
    """Read (cache reads, 5-minute cache writes, 1-hour cache writes).

    Anthropic reports these beside ``input_tokens`` rather than inside it, and
    bills them at their own rates. Absent fields count as zero. Writes are
    split by lifetime when the response says so, and otherwise priced as
    5-minute writes, the lifetime of the automatic cache.
    """
    usage = raw.get("usage")
    if not isinstance(usage, dict):
        return 0, 0, 0

    def count(value: Any) -> int:
        return value if isinstance(value, int) else 0

    read = count(usage.get("cache_read_input_tokens"))
    written = count(usage.get("cache_creation_input_tokens"))
    split = usage.get("cache_creation")
    if isinstance(split, dict):
        one_hour = count(split.get("ephemeral_1h_input_tokens"))
        five_minutes = count(split.get("ephemeral_5m_input_tokens"))
        if one_hour or five_minutes:
            return read, five_minutes, one_hour
    return read, written, 0


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
    read_rate = per_input * CACHE_READ_MULTIPLIER_BY_MODEL.get(model, CACHE_READ_MULTIPLIER)
    total = 0.0
    priced = False
    for raw in raws:
        usage = extract_usage(raw)
        if usage is None:
            continue
        priced = True
        got_in, got_out = usage
        read, write_5m, write_1h = extract_cache_usage(raw)
        total += (
            got_in * per_input
            + got_out * per_output
            + read * read_rate
            + write_5m * per_input * CACHE_WRITE_5M_MULTIPLIER
            + write_1h * per_input * CACHE_WRITE_1H_MULTIPLIER
        ) / 1_000_000
    if not priced:
        return None
    return round(total, 6)


def _import_sdk() -> Any:
    """The ``anthropic`` module, or the configuration error that says to install it."""
    try:
        import anthropic
    except ImportError as exc:
        raise AnthropicNotConfiguredError(
            "the 'anthropic' package is not installed; "
            'install it with: pip install -e ".[anthropic]"'
        ) from exc
    return anthropic


class AnthropicModelAdapter:
    """Adapter for Anthropic Claude models.

    Construction validates configuration, the model's sampling rules, the key
    and the SDK, so ``--provider anthropic`` fails fast with instructions
    before a run exists. ``next_action`` uses the pure helpers above.
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
        check_sampling(self.model, temperature)
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
        # Imported here so a missing SDK stops the run before it starts. Only
        # an Anthropic run builds this adapter, so nothing else needs it.
        self._sdk = _import_sdk()
        self._client_obj: anthropic.Anthropic | None = None

    def _client(self) -> anthropic.Anthropic:
        """Build the SDK client once, on the first call."""
        if self._client_obj is None:
            self._client_obj = self._sdk.Anthropic(
                api_key=self.api_key,
                # Seconds here, unlike Gemini's milliseconds. Complements the
                # runner's between-call timeout rather than replacing it.
                timeout=self.timeout_seconds,
            )
        return self._client_obj

    def _request(
        self, system: str | None, messages: list[AnthropicMessage], tools: list[ToolDefinition]
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": messages,
        }
        if system is not None:
            request["system"] = system
        if tools:
            request["tools"] = tools
            # One action per turn is the harness contract, so the model is told
            # to make at most one call. "auto" still lets it answer in text.
            request["tool_choice"] = {"type": "auto", "disable_parallel_tool_use": True}
        if self.temperature is not None:
            # anthropic 1.x dropped temperature from messages.create. The API
            # still takes it on the models check_sampling lets through.
            request["extra_body"] = {"temperature": self.temperature}
        return request

    def next_action(self, transcript: list[Message], tools: list[ToolSpec]) -> AgentAction:
        client = self._client()
        system, messages = _transcript_to_messages(transcript)
        request = self._request(system, messages, _tools_to_definitions(tools))
        try:
            response = client.messages.create(**request)
        except self._sdk.APIError as exc:
            # Map provider errors to the runner's clean model_error termination.
            raise ModelAdapterError(f"Anthropic API call failed: {exc}") from exc
        return _normalize_response(response)
