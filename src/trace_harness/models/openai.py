"""OpenAIModelAdapter, the third live provider (#160).

Why this exists alongside Anthropic
    #160 asked for one more vendor and set three requirements. Native tool
    calling with JSON-schema parameters, a seed or equivalent determinism
    setting, and a published per-token price. Anthropic meets two of the three
    and has no seed at all. #217 pre-registers at least five seeds per
    condition across two providers, so a provider whose seed is never sent
    makes that sample plan meaningless.

    This adapter meets all three. Gemini and Anthropic stay exactly as they
    are, and which provider a run uses is a run-configuration choice.

Design, the same as the other two
    The conversion helpers are pure and SDK-free, so they unit-test offline
    with no ``openai`` install, no key and no network. Only the constructor and
    ``next_action`` touch the SDK, and the tests stand a fake module in for it,
    so the request this adapter builds is checked offline too. The live path
    itself is verified by running a task with ``--provider openai``.

Where OpenAI differs from the other two
    Tool arguments arrive as a JSON string rather than an object, so they are
    parsed here. A string that will not parse is an adapter error, because a
    tool call whose arguments cannot be read is not a call the runner can
    dispatch or the verifier can judge.

    There is a real ``tool`` message role, which neither Gemini nor Anthropic
    has, and a tool message quotes the ``tool_call_id`` it answers. That id
    rides in ``provider_state`` the same way Anthropic's ``tool_use`` id does.

    ``seed`` is best-effort rather than a guarantee. The response carries a
    ``system_fingerprint`` that changes when the backend changed underneath a
    seeded request, so it is recorded on the action. A seeded run whose
    fingerprint moved is not a reproduction, and #195 is what will act on that.

    Reasoning models, the gpt-5 family among them, reject ``temperature``
    whenever their reasoning effort is anything but ``none``, and this adapter
    never sets an effort. A temperature configured for one of them is refused
    when the adapter is built, before any run exists, since sending it would
    fail every call and dropping it would record a setting the run never had.

    A tool call from a turn with no OpenAI id of its own, such as a scripted
    prefix replayed before a live continuation, gets a stable id made from its
    position, so the tool message after it still pairs. A turn cut off at the
    length limit is a model error, with the billed response kept in the trace.
    Prompt tokens served from the cache are priced at the cached rate.

Out of scope, the same as the other adapters: retries, backoff, rate limiting,
streaming. Parallel tool calls are switched off on the request, and a response
carrying two anyway is an error.
"""

from __future__ import annotations

import json
import os
import re
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
    import openai

DEFAULT_OPENAI_MODEL = "gpt-5"

#: Key under AgentAction.provider_state holding the id of the tool call this
#: turn produced. The tool message answering it has to quote the same id.
TOOL_CALL_ID_KEY = "tool_call_id"

#: Key holding the backend build that served a seeded request. A seeded run
#: whose fingerprint moved is not a reproduction of the earlier one.
SYSTEM_FINGERPRINT_KEY = "system_fingerprint"

#: USD per million tokens, keyed by model name. Data rather than logic, so a
#: price change is a one-line diff and an unpriced model is visibly absent.
#: Source: https://developers.openai.com/api/docs/pricing, checked 2026-09-24.
OPENAI_PRICING: dict[str, tuple[float, float]] = {
    # model: (input per million, output per million)
    "gpt-5": (1.25, 10.0),
    "gpt-5-mini": (0.25, 2.0),
    "gpt-4.1": (2.0, 8.0),
    "gpt-4.1-mini": (0.4, 1.6),
}

#: USD per million prompt tokens served from the cache, from the same page.
#: ``prompt_tokens`` counts them too, so they are priced here in place of the
#: full input rate.
OPENAI_CACHED_INPUT_PRICING: dict[str, float] = {
    "gpt-5": 0.125,
    "gpt-5-mini": 0.025,
    "gpt-4.1": 0.5,
    "gpt-4.1-mini": 0.1,
}

#: Models that reject a non-default temperature at the reasoning effort this
#: adapter runs them at, which is their default. OpenAI's model guidance says
#: to remove temperature, top_p and top_logprobs whenever the effort is not
#: ``none`` (https://developers.openai.com/api/docs/guides/latest-model). The
#: gpt-5 family offers minimal to high with no ``none``
#: (https://developers.openai.com/api/docs/models/gpt-5); gpt-5.5, gpt-6-sol and
#: gpt-6-luna default to ``medium`` and gpt-6-astra has no ``none``
#: (https://developers.openai.com/api/docs/guides/reasoning); the o-series are
#: reasoning models under the same rule. Checked 2026-09-24. A dated snapshot
#: such as gpt-5-2025-08-07 follows its base model.
FIXED_SAMPLING_MODELS = frozenset(
    {
        "gpt-5",
        "gpt-5-mini",
        "gpt-5-nano",
        "gpt-5.5",
        "gpt-6-astra",
        "gpt-6-sol",
        "gpt-6-luna",
        "o1",
        "o3",
        "o3-mini",
        "o4-mini",
    }
)

_SNAPSHOT_SUFFIX = re.compile(r"-\d{4}-\d{2}-\d{2}$")


class OpenAINotConfiguredError(ProviderNotConfiguredError):
    """Raised at construction time when the OpenAI adapter cannot be used."""


OpenAIMessage = dict[str, Any]
ToolDefinition = dict[str, Any]


def check_sampling(model: str, temperature: float | None) -> None:
    """Refuse a temperature the model would reject with a 400 on every call."""
    if temperature is not None and _SNAPSHOT_SUFFIX.sub("", model) in FIXED_SAMPLING_MODELS:
        raise OpenAINotConfiguredError(
            f"{model} is a reasoning model and rejects a non-default temperature at its "
            f"default reasoning effort, so temperature={temperature} cannot be sent. Leave "
            "temperature unset for this model, or choose one that accepts it such as "
            "gpt-4.1. See https://developers.openai.com/api/docs/guides/latest-model"
        )


def _synthetic_tool_call_id(position: int) -> str:
    """A stable id for a tool call that arrived without one.

    A scripted prefix, or a turn another provider produced, has no OpenAI id.
    The position in the transcript never changes once written, so the id is
    the same on every later request and the tool message can quote it.
    """
    return f"call_trace_{position}"


def _transcript_to_messages(transcript: list[Message]) -> list[OpenAIMessage]:
    """Convert the runner's transcript into OpenAI chat messages.

    The system prompt is a message here, unlike Gemini and Anthropic where it
    is a separate parameter, so this returns one list rather than a pair.

        SYSTEM    -> {"role": "system", "content": ...}
        USER      -> {"role": "user", "content": ...}
        ASSISTANT -> a tool_calls message when metadata carries a tool call,
                     with arguments re-encoded as the JSON string the API
                     expects, otherwise a content message
        TOOL      -> {"role": "tool", "tool_call_id": <id from the assistant
                     turn>, "content": ...}

    A tool message has to quote the id of the call it answers. That id arrives
    on the assistant turn before it, so the mapping carries the most recent one
    forward. A tool message with no call before it cannot be paired with
    anything, and is an adapter error here rather than a 400 mid-run.
    """
    messages: list[OpenAIMessage] = []
    pending_tool_call_id: str | None = None

    for position, msg in enumerate(transcript):
        if msg.role is MessageRole.SYSTEM:
            if msg.content:
                messages.append({"role": "system", "content": msg.content})
        elif msg.role is MessageRole.USER:
            messages.append({"role": "user", "content": msg.content})
        elif msg.role is MessageRole.ASSISTANT:
            tool_call = msg.metadata.get("tool_call")
            if tool_call:
                pending_tool_call_id = (msg.metadata.get("provider_state") or {}).get(
                    TOOL_CALL_ID_KEY
                ) or _synthetic_tool_call_id(position)
                messages.append(
                    {
                        "role": "assistant",
                        "content": msg.content or None,
                        "tool_calls": [
                            {
                                "id": pending_tool_call_id,
                                "type": "function",
                                "function": {
                                    "name": tool_call["tool_name"],
                                    "arguments": json.dumps(
                                        tool_call.get("arguments", {}), sort_keys=True
                                    ),
                                },
                            }
                        ],
                    }
                )
            else:
                messages.append({"role": "assistant", "content": msg.content})
        elif msg.role is MessageRole.TOOL:
            if pending_tool_call_id is None:
                raise ModelAdapterError(
                    "a tool result has no tool call before it in the transcript, so "
                    "there is no tool_call_id for it to quote"
                )
            md = msg.metadata
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": pending_tool_call_id,
                    "content": _tool_result_text(md.get("result"), md.get("error")),
                }
            )
            pending_tool_call_id = None
    return messages


def _tool_result_text(result: Any, error: str | None) -> str:
    """Render a tool result as message content.

    An error is sent as the content rather than dropped, because the agent
    recovering from a tool failure is behavior the verifier is entitled to
    judge. OpenAI has no error flag on a tool message, so the text is all there
    is to carry it.
    """
    if error:
        return str(error)
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    return json.dumps(result, sort_keys=True, default=str)


def _tools_to_definitions(tools: list[ToolSpec]) -> list[ToolDefinition]:
    """Convert ToolSpecs into OpenAI function-tool definitions.

    ``ToolSpec.parameters`` is already a JSON schema and drops into
    ``function.parameters``. A tool with no parameters still needs an object
    schema, since the API rejects a bare empty one.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters or {"type": "object", "properties": {}},
            },
        }
        for t in tools
    ]


def _normalize_response(response: Any) -> AgentAction:
    """Normalize an OpenAI response into exactly one AgentAction.

    Duck-typed on ``response.choices`` so a fake object stands in for the SDK's.

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

    More than one tool call is an error rather than a silent drop. TRACE
    attributes a failure to a step, and two actions recorded as one step would
    make that attribution point at something that never happened.
    """
    choices = list(getattr(response, "choices", None) or [])
    if not choices:
        raise ModelAdapterError("OpenAI returned no choices (empty response)")

    message = getattr(choices[0], "message", None)
    finish_reason = getattr(choices[0], "finish_reason", None)
    if finish_reason == "content_filter":
        raise ModelAdapterError("OpenAI stopped on a content filter")
    if finish_reason == "length":
        raise ModelAdapterError(
            "OpenAI stopped the turn at the token limit (finish_reason 'length'), so "
            "what came back is incomplete"
        )

    tool_calls = list(getattr(message, "tool_calls", None) or [])
    if len(tool_calls) > 1:
        raise ModelAdapterError(
            "OpenAI returned multiple tool calls, but TRACE requires exactly one action "
            "per turn; parallel tool calls are not supported"
        )

    fingerprint = getattr(response, "system_fingerprint", None)
    state: dict[str, Any] = {}
    if fingerprint:
        state[SYSTEM_FINGERPRINT_KEY] = fingerprint

    text = getattr(message, "content", None) or None
    if tool_calls:
        call = tool_calls[0]
        function = getattr(call, "function", None)
        call_id = getattr(call, "id", None)
        if not call_id:
            raise ModelAdapterError(
                "OpenAI returned a tool call with no id, so its result could never be "
                "paired with it"
            )
        state[TOOL_CALL_ID_KEY] = call_id
        return AgentAction(
            kind=ActionKind.TOOL_CALL,
            tool_call=ToolCall(
                tool_name=getattr(function, "name", ""),
                arguments=_parse_arguments(getattr(function, "arguments", None)),
            ),
            reasoning=text,
            raw=raw,
            provider_state=state or None,
        )
    if text:
        return AgentAction(
            kind=ActionKind.FINAL_ANSWER,
            final_answer=text,
            raw=raw,
            provider_state=state or None,
        )
    raise ModelAdapterError(
        "OpenAI returned neither a tool call nor content (empty or blocked response)"
    )


def _parse_arguments(arguments: Any) -> dict[str, Any]:
    """Decode the JSON string OpenAI sends tool arguments as.

    Unlike the other two providers this arrives as text. Arguments that will
    not parse, or that parse to something other than an object, are an error,
    because a call the runner cannot dispatch is worse than a turn that failed
    loudly.
    """
    if arguments in (None, ""):
        return {}
    if isinstance(arguments, dict):
        return arguments
    try:
        parsed = json.loads(arguments)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ModelAdapterError(
            f"OpenAI sent tool arguments that are not valid JSON: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise ModelAdapterError(
            f"OpenAI sent tool arguments that are not an object: {type(parsed).__name__}"
        )
    return parsed


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
    """Read (prompt_tokens, completion_tokens) out of a recorded raw response.

    Returns None when the response carries no usage, which is what a fixture or
    a cassette replay looks like. None and ``(0, 0)`` mean different things, so
    an absent usage block never becomes a zero cost. Reasoning tokens are part
    of ``completion_tokens`` and billed as output.
    """
    usage = raw.get("usage")
    if not isinstance(usage, dict):
        return None
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if not isinstance(prompt, int) or not isinstance(completion, int):
        return None
    return prompt, completion


def extract_cached_prompt_tokens(raw: dict[str, Any]) -> int:
    """How many of a response's prompt tokens were served from the cache, or 0."""
    usage = raw.get("usage")
    details = usage.get("prompt_tokens_details") if isinstance(usage, dict) else None
    cached = details.get("cached_tokens") if isinstance(details, dict) else None
    return cached if isinstance(cached, int) else 0


def estimate_cost_usd(model: str, raws: list[dict[str, Any]]) -> float | None:
    """Price every recorded response for ``model``, or None if it cannot be priced.

    An unpriced model returns None rather than 0.0, because a run that cost
    money and reports zero is worse than one that reports nothing. Cached
    prompt tokens are priced at the cached rate, or at the full input rate for
    a model without one, which can only overstate the cost.
    """
    price = OPENAI_PRICING.get(model)
    if price is None:
        return None
    per_input, per_output = price
    per_cached = OPENAI_CACHED_INPUT_PRICING.get(model, per_input)
    total = 0.0
    priced = False
    for raw in raws:
        usage = extract_usage(raw)
        if usage is None:
            continue
        priced = True
        prompt, completion = usage
        cached = min(extract_cached_prompt_tokens(raw), prompt)
        total += (
            (prompt - cached) * per_input + cached * per_cached + completion * per_output
        ) / 1_000_000
    if not priced:
        return None
    return round(total, 6)


def _import_sdk() -> Any:
    """The ``openai`` module, or the configuration error that says to install it."""
    try:
        import openai
    except ImportError as exc:
        raise OpenAINotConfiguredError(
            "the 'openai' package is not installed; install it with: pip install -e \".[openai]\""
        ) from exc
    return openai


class OpenAIModelAdapter:
    """Adapter for OpenAI chat models.

    Construction validates configuration, the model's sampling rules, the key
    and the SDK, so ``--provider openai`` fails fast with instructions before a
    run exists. ``next_action`` uses the pure helpers above.
    """

    name = "openai"

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        *,
        temperature: float | None = None,
        seed: int | None = None,
        timeout_seconds: float = 120.0,
    ):
        self.model = model or DEFAULT_OPENAI_MODEL
        check_sampling(self.model, temperature)
        self.temperature = temperature
        # Sent, unlike the Anthropic adapter. Best-effort on the provider's
        # side, which is what system_fingerprint exists to expose.
        self.seed = seed
        self.timeout_seconds = timeout_seconds
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        if not self.api_key:
            raise OpenAINotConfiguredError(
                "OPENAI_API_KEY is not set. Get a key from "
                "https://platform.openai.com/api-keys, put it in your local "
                ".env (see .env.example), and re-run. The fixture provider "
                "(TRACE_MODEL_PROVIDER=fixture) needs no key and is the "
                "default for all tests and CI."
            )
        # Imported here so a missing SDK stops the run before it starts. Only
        # an OpenAI run builds this adapter, so nothing else needs it.
        self._sdk = _import_sdk()
        self._client_obj: openai.OpenAI | None = None

    def _client(self) -> openai.OpenAI:
        """Build the SDK client once, on the first call."""
        if self._client_obj is None:
            self._client_obj = self._sdk.OpenAI(
                api_key=self.api_key,
                # Seconds, matching the Anthropic client. Complements the
                # runner's between-call timeout rather than replacing it.
                timeout=self.timeout_seconds,
            )
        return self._client_obj

    def next_action(self, transcript: list[Message], tools: list[ToolSpec]) -> AgentAction:
        client = self._client()
        messages = _transcript_to_messages(transcript)
        definitions = _tools_to_definitions(tools)

        request: dict[str, Any] = {"model": self.model, "messages": messages}
        if definitions:
            request["tools"] = definitions
            # One action per turn is the harness contract, so the provider is
            # told not to batch calls rather than having them dropped here.
            request["parallel_tool_calls"] = False
        if self.temperature is not None:
            request["temperature"] = self.temperature
        if self.seed is not None:
            request["seed"] = self.seed

        try:
            response = client.chat.completions.create(**request)
        except self._sdk.OpenAIError as exc:
            # Map provider errors to the runner's clean model_error termination.
            raise ModelAdapterError(f"OpenAI API call failed: {exc}") from exc
        return _normalize_response(response)
