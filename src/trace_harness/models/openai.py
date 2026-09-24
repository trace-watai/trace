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
    with no ``openai`` install, no key and no network. Only ``next_action``
    touches the live SDK, and that path is verified by running a task with
    ``--provider openai`` rather than in the suite.

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

Retries, backoff and the rate limit come from the shared policy in
``models/policy.py`` (#196). The SDK's own two default retries are switched off
with ``max_retries=0``, so every attempt is one the policy made and recorded. A
429 whose code is ``insufficient_quota`` is permanent here: the account is out
of credit, and no retry within a run brings it back.

Out of scope, the same as the other adapters: parallel tool calls, streaming.
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
from trace_harness.models.policy import (
    CallPolicy,
    ErrorVerdict,
    LiveCaller,
    classify_provider_error,
    default_call_policy,
    with_call_record,
)

if TYPE_CHECKING:  # typing only, the runtime import stays lazy inside methods
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
OPENAI_PRICING: dict[str, tuple[float, float]] = {
    # model: (input per million, output per million)
    "gpt-5": (1.25, 10.0),
    "gpt-5-mini": (0.25, 2.0),
    "gpt-4.1": (2.0, 8.0),
    "gpt-4.1-mini": (0.4, 1.6),
}


#: The SDK error base this adapter has always mapped to a model error. A
#: status-less error under it is still the provider's, and permanent.
_SDK_ERROR_NAMES = frozenset({"OpenAIError"})


class OpenAINotConfiguredError(ProviderNotConfiguredError):
    """Raised at construction time when the OpenAI adapter cannot be used."""


OpenAIMessage = dict[str, Any]
ToolDefinition = dict[str, Any]


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
    forward.
    """
    messages: list[OpenAIMessage] = []
    pending_tool_call_id: str | None = None

    for msg in transcript:
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
                ) or ""
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
            md = msg.metadata
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": pending_tool_call_id or "",
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

    More than one tool call is an error rather than a silent drop. TRACE
    attributes a failure to a step, and two actions recorded as one step would
    make that attribution point at something that never happened.
    """
    raw = _response_to_dict(response)
    choices = list(getattr(response, "choices", None) or [])
    if not choices:
        raise ModelAdapterError("OpenAI returned no choices (empty response)")

    message = getattr(choices[0], "message", None)
    finish_reason = getattr(choices[0], "finish_reason", None)
    if finish_reason == "content_filter":
        raise ModelAdapterError("OpenAI stopped on a content filter")

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
        if call_id:
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
    an absent usage block never becomes a zero cost.
    """
    usage = raw.get("usage")
    if not isinstance(usage, dict):
        return None
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if not isinstance(prompt, int) or not isinstance(completion, int):
        return None
    return prompt, completion


def estimate_cost_usd(model: str, raws: list[dict[str, Any]]) -> float | None:
    """Price every recorded response for ``model``, or None if it cannot be priced.

    An unpriced model returns None rather than 0.0, because a run that cost
    money and reports zero is worse than one that reports nothing.
    """
    price = OPENAI_PRICING.get(model)
    if price is None:
        return None
    per_input, per_output = price
    usages = [usage for raw in raws if (usage := extract_usage(raw)) is not None]
    if not usages:
        return None
    total = sum(
        (prompt * per_input + completion * per_output) / 1_000_000 for prompt, completion in usages
    )
    return round(total, 6)


def classify_error(exc: Exception) -> ErrorVerdict | None:
    """OpenAI's errors under the shared rules in ``models/policy.py``.

    A 429 is usually a rate limit and transient. With the error code
    ``insufficient_quota`` it means the account has no credit left, which does
    not recover within a run, so it is permanent.
    """
    verdict = classify_provider_error(exc, sdk_error_names=_SDK_ERROR_NAMES)
    if verdict is not None and getattr(exc, "code", None) == "insufficient_quota":
        return verdict.permanent()
    return verdict


class OpenAIModelAdapter:
    """Adapter for OpenAI chat models.

    Construction validates configuration, so ``--provider openai`` fails fast
    with instructions. ``next_action`` uses the pure helpers above and keeps
    the optional SDK import at the live-call boundary.
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
        call_policy: CallPolicy | None = None,
        caller: LiveCaller | None = None,
    ):
        self.model = model or DEFAULT_OPENAI_MODEL
        self.temperature = temperature
        # Sent, unlike the Anthropic adapter. Best-effort on the provider's
        # side, which is what system_fingerprint exists to expose.
        self.seed = seed
        self.timeout_seconds = timeout_seconds
        self._caller = caller or LiveCaller(
            self.name,
            call_policy or default_call_policy(self.name),
            budget_seconds=timeout_seconds,
        )
        self.call_policy = self._caller.policy
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        if not self.api_key:
            raise OpenAINotConfiguredError(
                "OPENAI_API_KEY is not set. Get a key from "
                "https://platform.openai.com/api-keys, put it in your local "
                ".env (see .env.example), and re-run. The fixture provider "
                "(TRACE_MODEL_PROVIDER=fixture) needs no key and is the "
                "default for all tests and CI."
            )
        self._client_obj: openai.OpenAI | None = None

    def _client(self) -> openai.OpenAI:
        """Lazily build the SDK client, so a non-OpenAI run never needs the
        ``openai`` package installed."""
        if self._client_obj is None:
            try:
                import openai
            except ImportError as exc:  # pragma: no cover - optional dependency
                raise OpenAINotConfiguredError(
                    "the 'openai' package is not installed; "
                    'install it with: pip install -e ".[openai]"'
                ) from exc
            self._client_obj = openai.OpenAI(
                api_key=self.api_key,
                # Seconds, matching the Anthropic client. Complements the
                # runner's between-call timeout rather than replacing it.
                timeout=self.timeout_seconds,
                # The policy owns retries, so each attempt is recorded.
                max_retries=0,
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

        # A provider error the policy gives up on is a ProviderCallError, the
        # runner's clean model_error termination, with the attempts attached.
        response, record = self._caller.call(
            lambda: client.chat.completions.create(**request), classify_error
        )
        return with_call_record(
            record,
            lambda: _normalize_response(response),
            raw=lambda: _response_to_dict(response),
        )
