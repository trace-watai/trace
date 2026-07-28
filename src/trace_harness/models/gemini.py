"""GeminiModelAdapter — the first *real* model adapter (scaffold + helper seams).

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
    - ``_normalize_response`` reads attributes (``.function_calls``, ``.text``)
      off the response, so a duck-typed fake stands in for the real object.

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
        model="gemini-2.0-flash",
        contents=[{"role": "user", "parts": [{"text": "..."}]}, ...],
        config=types.GenerateContentConfig(
            system_instruction="...",
            tools=[types.Tool(function_declarations=[{...}])],
            temperature=0.0,
        ),
    )
    resp.function_calls  # list[FunctionCall] with .name / .args
    resp.text            # str | None

Out of scope (separate work): retries, rate limiting, cost tracking, parallel
tool calls, streaming.

# TODO(Rupert/runner): JSON tool-mode fallback for providers/models without
# native function calling; verify HttpOptions.timeout units against the pinned
# google-genai version; confirm function-response content role with the SDK.
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
    ToolCall,
    ToolSpec,
)

if TYPE_CHECKING:  # typing only — the runtime import stays lazy inside methods
    from google import genai

DEFAULT_GEMINI_MODEL = "gemini-2.0-flash"


class GeminiNotConfiguredError(RuntimeError):
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
                              {"name": tc["tool_name"], "args": tc["arguments"]}}]}
                      else (final answer / reasoning text):
                          {"role": "model", "parts": [{"text": msg.content}]}
        TOOL       -> {"role": "tool", "parts": [{"function_response":
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
                contents.append(
                    {
                        "role": "model",
                        "parts": [
                            {
                                "function_call": {
                                    "name": tool_call["tool_name"],
                                    "args": tool_call.get("arguments", {}),
                                }
                            }
                        ],
                    }
                )
            else:
                contents.append({"role": "model", "parts": [{"text": msg.content}]})
        elif msg.role is MessageRole.TOOL:
            md = msg.metadata
            contents.append(
                {
                    "role": "tool",
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
    if function_calls:
        call = function_calls[0]
        return AgentAction(
            kind=ActionKind.TOOL_CALL,
            tool_call=ToolCall(tool_name=call.name, arguments=dict(call.args or {})),
            reasoning=_safe_text(response) or None,  # text alongside a call, if any
            raw=raw,
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


class GeminiModelAdapter:
    """Adapter for Google Gemini models.

    Construction validates configuration (so ``--provider gemini`` fails fast,
    with instructions). ``next_action`` is wired; the conversion helpers it
    calls are the unimplemented seams.
    """

    name = "gemini"

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        *,
        temperature: float | None = None,
        timeout_seconds: float = 120.0,
    ):
        self.model = model or DEFAULT_GEMINI_MODEL
        self.temperature = temperature
        self.timeout_seconds = timeout_seconds
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
                # daemon-thread timeout (TRA-58).
                http_options=types.HttpOptions(timeout=int(self.timeout_seconds * 1000)),
            )
        return self._client_obj

    def next_action(self, transcript: list[Message], tools: list[ToolSpec]) -> AgentAction:
        client = self._client()
        system, contents = _transcript_to_contents(transcript)
        declarations = _tools_to_declarations(tools)

        from google.genai import errors as genai_errors
        from google.genai import types

        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=[types.Tool(function_declarations=declarations)] if declarations else None,
            temperature=self.temperature,
        )
        try:
            response = client.models.generate_content(
                model=self.model,
                contents=contents,
                config=config,
            )
        except genai_errors.APIError as exc:
            # Map provider errors to the runner's clean model_error termination.
            raise ModelAdapterError(f"Gemini API call failed: {exc}") from exc
        return _normalize_response(response)
