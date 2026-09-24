"""GeminiModelAdapter — the first real provider adapter.

Why this exists now
    The team plans to prototype with free-tier Gemini API keys before sponsor
    credits land. This adapter pins down the contract (same normalized
    :class:`AgentAction` as the fixture adapter) so the runner, verifiers, and
    dashboard never have to care which provider produced a trace.

Design: keep the SDK at the edges
    The three conversion helpers below are *pure* and SDK-free so they unit-test
    offline with no ``google-genai`` install and no network:

    - ``_transcript_to_contents`` / ``_tools_to_declarations`` return plain
      dicts; ``generate_content`` coerces dicts into ``types.Content`` /
      ``types.Tool`` for us.
    - ``_normalize_response`` reads documented response attributes, so a
      duck-typed fake stands in for the real object.

    Only ``next_action`` itself touches the live SDK; that path is verified
    manually (run a task with ``--provider gemini``), never in the test suite —
    tests are offline forever.

Current status — implemented (native function calling)
    ``next_action`` and the three conversion helpers are implemented for
    ``tool_mode = native``. The pure helpers are covered offline in
    ``tests/test_gemini_adapter.py``; the live ``generate_content`` path is
    verified by running a task with ``--provider gemini`` (never in tests).
    JSON tool-mode fallback is not implemented yet.

google-genai API reference (verify against the pinned version while implementing):
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=..., http_options=types.HttpOptions(timeout=ms))
    resp = client.models.generate_content(
        model="gemini-3.6-flash",
        contents=[{"role": "user", "parts": [{"text": "..."}]}, ...],
        config=types.GenerateContentConfig(
            system_instruction="...",
            tools=[types.Tool(function_declarations=[{...}])],
            temperature=0.0,
        ),
    )
    resp.function_calls  # list[FunctionCall] with .name / .args
    resp.text            # str | None

Retries, backoff and the rate limit come from the shared policy in
``models/policy.py`` (#196), which the request goes through. The request config
is a plain dict that the SDK validates into ``types.GenerateContentConfig``, so
the whole call path runs offline against a fake client. Token usage is read
from ``usage_metadata`` and priced from ``GEMINI_PRICING``.

Out of scope (separate work): parallel tool calls, streaming.

# TODO(Rupert/runner): JSON tool-mode fallback for providers/models without
# native function calling.
"""

from __future__ import annotations

import base64
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

if TYPE_CHECKING:  # typing only — the runtime import stays lazy inside methods
    from google import genai

DEFAULT_GEMINI_MODEL = "gemini-3.6-flash"

# Key under AgentAction.provider_state / Message.metadata["provider_state"]
# holding the base64-encoded thought signature Gemini attached to a
# function-call part. Stored as text so it survives the JSON trace.
THOUGHT_SIGNATURE_KEY = "thought_signature"

#: USD per million tokens, keyed by model name, as (input, output). Kept as data,
#: so a price change is a one-line diff and an unpriced model is visibly
#: absent. Output includes thinking tokens, which Gemini bills at the
#: output rate. Only models with one flat text price are listed; a model whose
#: price is unknown here, the default included, reports a null cost until its
#: line is added.
#: USD per million input and output tokens, paid tier, from
#: https://ai.google.dev/gemini-api/docs/pricing as read on 2026-09-23. Thinking
#: tokens bill as output. The gemini-3.6-flash price doubles to (1.50, 7.50) on
#: 2027-01-01, and this line has to change that day.
GEMINI_PRICING: dict[str, tuple[float, float]] = {
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-3.6-flash": (0.75, 3.75),
}

#: google-genai's error base class. A status-less error under it is still the
#: provider's, and permanent.
_SDK_ERROR_NAMES = frozenset({"APIError"})


class GeminiNotConfiguredError(ProviderNotConfiguredError):
    """Raised at construction time when the Gemini adapter cannot be used."""


# A Gemini "content" and "function declaration" in dict form (the SDK coerces
# these into types.Content / types.FunctionDeclaration). Kept as dicts so the
# converters stay pure and SDK-free for offline testing.
GeminiContent = dict[str, Any]
FunctionDeclaration = dict[str, Any]


def _transcript_to_contents(
    transcript: list[Message],
) -> tuple[str | None, list[GeminiContent]]:
    """Convert the runner's transcript into (system_instruction, contents).

    Gemini takes the system prompt *separately* (config.system_instruction),
    not as a content role — so collect SYSTEM messages into the returned string
    and emit the rest as contents.

    Target mapping (read structured tool data from ``Message.metadata``, which
    is where the runner now puts it — see TRA-58):

        USER       -> {"role": "user",  "parts": [{"text": msg.content}]}
        ASSISTANT  -> if metadata["tool_call"]:
                          {"role": "model", "parts": [{"function_call":
                              {"name": tc["tool_name"], "args": tc["arguments"]},
                            "thought_signature": <bytes, if the provider_state
                              recorded one for this turn>}]}
                      else (final answer / reasoning text):
                          {"role": "model", "parts": [{"text": msg.content}]}
        TOOL       -> {"role": "user", "parts": [{"function_response":
                          {"name": metadata["tool_name"],
                           "response": {"result": metadata["result"],
                                        "error": metadata["error"]}}}]}

    Multiple SYSTEM messages join with newlines; absent system -> None.
    """
    system_parts: list[str] = []
    contents: list[GeminiContent] = []
    for msg in transcript:
        if msg.role is MessageRole.SYSTEM:
            if msg.content:
                system_parts.append(msg.content)
        elif msg.role is MessageRole.USER:
            contents.append({"role": "user", "parts": [{"text": msg.content}]})
        elif msg.role is MessageRole.ASSISTANT:
            tool_call = msg.metadata.get("tool_call")
            if tool_call:
                part: dict[str, Any] = {
                    "function_call": {
                        "name": tool_call["tool_name"],
                        "args": tool_call.get("arguments", {}),
                    }
                }
                # Gemini 3 models require the thought_signature that arrived on
                # the function-call part to be sent back on that same part, or
                # the next call fails with 400 INVALID_ARGUMENT (TRA-81).
                signature = (msg.metadata.get("provider_state") or {}).get(THOUGHT_SIGNATURE_KEY)
                if signature:
                    part["thought_signature"] = base64.b64decode(signature)
                contents.append({"role": "model", "parts": [part]})
            else:
                contents.append({"role": "model", "parts": [{"text": msg.content}]})
        elif msg.role is MessageRole.TOOL:
            # Gemini has no "tool" content role: function responses are sent
            # back under the "user" role (the API rejects "tool" with
            # 400 INVALID_ARGUMENT, observed live on TRA-81). The part type
            # (function_response) is what tells Gemini this is a tool result.
            md = msg.metadata
            contents.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "function_response": {
                                "name": md.get("tool_name", ""),
                                "response": {
                                    "result": md.get("result"),
                                    "error": md.get("error"),
                                },
                            }
                        }
                    ],
                }
            )
    system = "\n".join(system_parts) if system_parts else None
    return system, contents


def _tools_to_declarations(tools: list[ToolSpec]) -> list[FunctionDeclaration]:
    """Convert ToolSpecs into Gemini function declarations (dict form).

    Each ToolSpec.parameters is already a JSON-schema dict, so it drops
    straight into ``parameters_json_schema``:

        {"name": t.name,
         "description": t.description,
         "parameters_json_schema": t.parameters}

    Return [] when there are no tools (caller passes no Tool to the config).
    """
    return [
        {
            "name": t.name,
            "description": t.description,
            "parameters_json_schema": t.parameters,
        }
        for t in tools
    ]


def _generate_config(
    system: str | None,
    declarations: list[FunctionDeclaration],
    *,
    temperature: float | None,
    seed: int | None,
) -> dict[str, Any]:
    """The ``GenerateContentConfig`` for one request, in dict form.

    ``generate_content`` validates a dict config into the same model the typed
    constructor builds, so this is the same request without the SDK import.
    Automatic function calling stays disabled, because tools run in the
    harness environment.
    """
    config: dict[str, Any] = {"automatic_function_calling": {"disable": True}}
    if system is not None:
        config["system_instruction"] = system
    if declarations:
        config["tools"] = [{"function_declarations": declarations}]
    if temperature is not None:
        config["temperature"] = temperature
    if seed is not None:
        config["seed"] = seed
    return config


def _normalize_response(response: Any) -> AgentAction:
    """Normalize a Gemini response into exactly one AgentAction.

    Duck-typed on purpose (``response.function_calls`` / ``response.text``) so
    tests can pass a fake object — no SDK needed.

        - response.function_calls non-empty -> AgentAction(
              kind=TOOL_CALL,
              tool_call=ToolCall(tool_name=fc.name, arguments=dict(fc.args or {})),
              reasoning=response.text or None,   # text alongside a call, if any
              raw=<response as dict>)
        - else response.text -> AgentAction(
              kind=FINAL_ANSWER, final_answer=response.text, raw=<response as dict>)
        - neither -> raise ModelAdapterError (empty/blocked response)

    For ``raw``, prefer ``response.model_dump(mode="json")`` when available,
    else best-effort ``dict(response)`` / ``{}``.
    """
    raw = _response_to_dict(response)
    function_calls = getattr(response, "function_calls", None)
    if function_calls and len(function_calls) != 1:
        raise ModelAdapterError(
            "Gemini returned multiple function calls, but TRACE requires exactly one action "
            "per turn; parallel tool calls are not supported"
        )
    if function_calls:
        call = function_calls[0]
        signature = _thought_signature_from_candidate_parts(response)
        return AgentAction(
            kind=ActionKind.TOOL_CALL,
            tool_call=ToolCall(tool_name=call.name, arguments=dict(call.args or {})),
            reasoning=_text_from_candidate_parts(response),
            raw=raw,
            provider_state={THOUGHT_SIGNATURE_KEY: signature} if signature else None,
        )
    text = _safe_text(response)
    if text:
        return AgentAction(kind=ActionKind.FINAL_ANSWER, final_answer=text, raw=raw)
    raise ModelAdapterError(
        "Gemini returned neither a function call nor text (empty or blocked response)"
    )


def _safe_text(response: Any) -> str | None:
    """Read ``response.text`` defensively.

    The SDK's ``.text`` property can warn or raise when the response holds only
    function-call parts; we never want that to crash normalization.
    """
    try:
        return response.text
    except Exception:  # noqa: BLE001 - any provider-side text accessor failure is non-fatal
        return None


def _text_from_candidate_parts(response: Any) -> str | None:
    """Read text that accompanies a function call without touching ``response.text``.

    The SDK warns when its convenience ``.text`` property sees non-text parts.
    Walking candidate parts avoids that warning while preserving any genuine
    text returned beside the single function call.
    """
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return None
    content = getattr(candidates[0], "content", None)
    parts = getattr(content, "parts", None) or []
    text = "\n".join(part_text for part in parts if (part_text := getattr(part, "text", None)))
    return text or None


def _thought_signature_from_candidate_parts(response: Any) -> str | None:
    """Return the base64 text of the thought signature on the function-call part.

    Gemini 3 attaches a ``thought_signature`` (bytes) to the part carrying the
    function call and requires it to be echoed back on the next request.
    Encoded to base64 text so it can live in the JSON trace and transcript.
    Returns None when the response carries no signature (older models, fakes).
    """
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return None
    content = getattr(candidates[0], "content", None)
    parts = getattr(content, "parts", None) or []
    for part in parts:
        if getattr(part, "function_call", None) is None:
            continue
        signature = getattr(part, "thought_signature", None)
        if signature:
            if isinstance(signature, str):
                return signature
            return base64.b64encode(bytes(signature)).decode("ascii")
    return None


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


def _retry_delay_seconds(exc: BaseException) -> float | None:
    """Gemini's own retry hint, from the ``RetryInfo`` detail on a 429.

    Gemini says when to come back in the error body (``"retryDelay": "17s"``)
    and often sends no ``Retry-After`` header, so this is its equivalent.
    """
    details = getattr(exc, "details", None)
    error = details.get("error") if isinstance(details, dict) else None
    items = error.get("details") if isinstance(error, dict) else None
    for item in items if isinstance(items, list) else []:
        delay = item.get("retryDelay") if isinstance(item, dict) else None
        if isinstance(delay, str) and delay.endswith("s"):
            try:
                seconds = float(delay[:-1])
            except ValueError:
                continue
            if seconds >= 0:
                return seconds
    return None


def classify_error(exc: Exception) -> ErrorVerdict | None:
    """Gemini's errors under the shared rules, with its ``retryDelay`` as the hint.

    google-genai reports the HTTP status as ``code`` and lets httpx transport
    errors through unwrapped; the shared classifier reads both.
    """
    verdict = classify_provider_error(exc, sdk_error_names=_SDK_ERROR_NAMES)
    if verdict is None or verdict.retry_after_seconds is not None:
        return verdict
    hint = _retry_delay_seconds(exc)
    return verdict if hint is None else ErrorVerdict(verdict.transient, verdict.status_code, hint)


def extract_usage(raw: dict[str, Any]) -> tuple[int, int] | None:
    """Read (input, output) tokens out of a recorded raw response.

    Input is ``prompt_token_count`` plus any ``tool_use_prompt_token_count``.
    Output is ``candidates_token_count`` plus ``thoughts_token_count``, since
    thinking is billed as output. Returns None when the response carries no
    usage, which is what a fixture looks like; None and ``(0, 0)`` mean
    different things, so an absent block never becomes a zero cost.
    """
    usage = raw.get("usage_metadata")
    if not isinstance(usage, dict):
        return None
    prompt = usage.get("prompt_token_count")
    candidates = usage.get("candidates_token_count")
    if not _is_count(prompt) or not _is_count(candidates):
        return None
    tool_prompt = usage.get("tool_use_prompt_token_count")
    thoughts = usage.get("thoughts_token_count")
    return (
        prompt + (tool_prompt if _is_count(tool_prompt) else 0),
        candidates + (thoughts if _is_count(thoughts) else 0),
    )


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def estimate_cost_usd(model: str, raws: list[dict[str, Any]]) -> float | None:
    """Price every recorded response for ``model``, or None if it cannot be priced.

    An unpriced model returns None, and so does a run with no recorded usage,
    so a cost that was never measured is never reported as zero.
    """
    price = GEMINI_PRICING.get(model)
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


class GeminiModelAdapter:
    """Adapter for Google Gemini models.

    Construction validates configuration (so ``--provider gemini`` fails fast,
    with instructions). ``next_action`` uses the pure conversion helpers above
    and keeps the optional SDK import at the live-call boundary.
    """

    name = "gemini"

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
        self.model = model or DEFAULT_GEMINI_MODEL
        self.temperature = temperature
        self.seed = seed
        self.timeout_seconds = timeout_seconds
        # Every request goes through the shared policy. A test injects a
        # caller with a fake clock; otherwise one is built from the policy.
        self._caller = caller or LiveCaller(
            self.name,
            call_policy or default_call_policy(self.name),
            budget_seconds=timeout_seconds,
        )
        self.call_policy = self._caller.policy
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        if not self.api_key:
            raise GeminiNotConfiguredError(
                "GEMINI_API_KEY is not set. Get a free key from "
                "https://aistudio.google.com/apikey, put it in your local "
                ".env (see .env.example), and re-run. The fixture provider "
                "(TRACE_MODEL_PROVIDER=fixture) needs no key and is the "
                "default for all tests and CI."
            )
        self._client_obj: genai.Client | None = None

    def _client(self) -> genai.Client:
        """Lazily build the SDK client (import is deferred so non-Gemini runs
        never need ``google-genai`` installed)."""
        if self._client_obj is None:
            try:
                from google import genai
                from google.genai import types
            except ImportError as exc:  # pragma: no cover - optional dependency
                raise GeminiNotConfiguredError(
                    "the 'google-genai' package is not installed; "
                    'install it with: pip install -e ".[gemini]"'
                ) from exc
            self._client_obj = genai.Client(
                api_key=self.api_key,
                # HttpOptions.timeout is milliseconds — verify against the
                # pinned SDK version. Complements the runner's between-call
                # daemon-thread timeout (TRA-58). retry_options stays unset,
                # so the SDK makes one attempt and every retry is the policy's.
                http_options=types.HttpOptions(timeout=int(self.timeout_seconds * 1000)),
            )
        return self._client_obj

    def next_action(self, transcript: list[Message], tools: list[ToolSpec]) -> AgentAction:
        client = self._client()
        system, contents = _transcript_to_contents(transcript)
        config = _generate_config(
            system,
            _tools_to_declarations(tools),
            temperature=self.temperature,
            seed=self.seed,
        )
        # A provider error the policy gives up on is a ProviderCallError, the
        # runner's clean model_error termination, with the attempts attached.
        response, record = self._caller.call(
            lambda: client.models.generate_content(
                model=self.model, contents=contents, config=config
            ),
            classify_error,
        )
        return with_call_record(
            record,
            lambda: _normalize_response(response),
            raw=lambda: _response_to_dict(response),
        )
