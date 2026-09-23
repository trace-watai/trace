"""Offline tests for the Gemini adapter's pure logic.

These never touch the network or need an API key (repo-wide rule). The
conversion helpers return plain dicts and ``_normalize_response`` is duck-typed,
so a tiny fake response stands in for the real SDK object — no ``google-genai``
install required. The request itself is built as a dict, so ``next_action``
runs against a fake client in ``tests/test_live_call_policy.py``. Only a real
``generate_content`` call needs the SDK, and that is checked by hand with
``--provider gemini`` outside the suite.
"""

from __future__ import annotations

import base64

import pytest

from trace_harness.models import estimate_cost_usd, is_priced
from trace_harness.models.base import ActionKind, Message, MessageRole, ModelAdapterError, ToolSpec
from trace_harness.models.gemini import (
    DEFAULT_GEMINI_MODEL,
    GEMINI_PRICING,
    THOUGHT_SIGNATURE_KEY,
    GeminiModelAdapter,
    GeminiNotConfiguredError,
    _generate_config,
    _normalize_response,
    _tools_to_declarations,
    _transcript_to_contents,
    extract_usage,
)
from trace_harness.models.gemini import estimate_cost_usd as gemini_cost

# --- construction (active: no SDK or key call needed) ---


def test_construction_without_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(GeminiNotConfiguredError):
        GeminiModelAdapter()


def test_construction_with_key_sets_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-used")
    adapter = GeminiModelAdapter()
    assert adapter.name == "gemini"
    assert adapter.model == DEFAULT_GEMINI_MODEL == "gemini-3.6-flash"


# --- _tools_to_declarations ---


def test_tools_to_declarations_maps_json_schema() -> None:
    params = {"type": "object", "properties": {"order_id": {"type": "integer"}}}
    tools = [ToolSpec(name="get_order", description="Look up an order", parameters=params)]

    decls = _tools_to_declarations(tools)

    assert decls == [
        {
            "name": "get_order",
            "description": "Look up an order",
            "parameters_json_schema": params,
        }
    ]


def test_tools_to_declarations_empty() -> None:
    assert _tools_to_declarations([]) == []


# --- _transcript_to_contents ---


def test_transcript_to_contents_maps_roles_and_extracts_system() -> None:
    transcript = [
        Message(role=MessageRole.SYSTEM, content="You are a support agent."),
        Message(role=MessageRole.USER, content="I want a refund."),
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            metadata={
                "kind": "tool_call",
                "tool_call": {"tool_name": "get_order", "arguments": {"order_id": 42}},
            },
        ),
        Message(
            role=MessageRole.TOOL,
            content="",
            metadata={
                "tool_name": "get_order",
                "status": "ok",
                "result": {"amount": 432},
                "error": None,
            },
        ),
    ]

    system, contents = _transcript_to_contents(transcript)

    assert system == "You are a support agent."
    assert contents[0] == {"role": "user", "parts": [{"text": "I want a refund."}]}
    assert contents[1] == {
        "role": "model",
        "parts": [{"function_call": {"name": "get_order", "args": {"order_id": 42}}}],
    }
    # Gemini rejects a "tool" role with 400 INVALID_ARGUMENT; function
    # responses must be sent under the "user" role (TRA-81 live acceptance).
    assert contents[2]["role"] == "user"
    assert contents[2]["parts"][0]["function_response"]["name"] == "get_order"
    assert "tool" not in {c["role"] for c in contents}


def test_transcript_to_contents_echoes_thought_signature_on_function_call() -> None:
    # Gemini 3 requires the thought_signature it attached to a function-call
    # part to come back on that same part (TRA-81 live acceptance).
    raw_sig = bytes([1, 2]) + b"signature-bytes" + bytes([255])
    transcript = [
        Message(role=MessageRole.USER, content="refund please"),
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            metadata={
                "kind": "tool_call",
                "tool_call": {"tool_name": "get_order", "arguments": {}},
                "provider_state": {
                    THOUGHT_SIGNATURE_KEY: base64.b64encode(raw_sig).decode("ascii")
                },
            },
        ),
    ]

    _, contents = _transcript_to_contents(transcript)

    part = contents[1]["parts"][0]
    assert part["function_call"] == {"name": "get_order", "args": {}}
    assert part["thought_signature"] == raw_sig


def test_transcript_to_contents_omits_thought_signature_when_absent() -> None:
    transcript = [
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            metadata={
                "kind": "tool_call",
                "tool_call": {"tool_name": "get_order", "arguments": {}},
            },
        ),
    ]
    _, contents = _transcript_to_contents(transcript)
    assert "thought_signature" not in contents[0]["parts"][0]


def test_transcript_to_contents_no_system_returns_none() -> None:
    transcript = [Message(role=MessageRole.USER, content="hi")]
    system, contents = _transcript_to_contents(transcript)
    assert system is None
    assert contents == [{"role": "user", "parts": [{"text": "hi"}]}]


# --- _normalize_response (duck-typed fake; no SDK) ---


class _FakeFunctionCall:
    def __init__(self, name: str, args: dict) -> None:
        self.name = name
        self.args = args


class _FakePart:
    def __init__(self, *, function_call=None, text=None, thought_signature=None) -> None:
        self.function_call = function_call
        self.text = text
        self.thought_signature = thought_signature


class _FakeContent:
    def __init__(self, parts: list) -> None:
        self.parts = parts


class _FakeCandidate:
    def __init__(self, parts: list) -> None:
        self.content = _FakeContent(parts)


class _FakeResponse:
    """Stands in for google.genai's GenerateContentResponse."""

    def __init__(
        self,
        *,
        function_calls: list | None = None,
        text: str | None = None,
        parts: list | None = None,
    ) -> None:
        self.function_calls = function_calls or []
        self.text = text
        self.candidates = [_FakeCandidate(parts)] if parts is not None else []

    def model_dump(self, mode: str = "json") -> dict:
        return {"function_calls": bool(self.function_calls), "text": self.text}


def test_normalize_response_tool_call() -> None:
    resp = _FakeResponse(function_calls=[_FakeFunctionCall("get_order", {"order_id": 42})])

    action = _normalize_response(resp)

    assert action.kind is ActionKind.TOOL_CALL
    assert action.tool_call is not None
    assert action.tool_call.tool_name == "get_order"
    assert action.tool_call.arguments == {"order_id": 42}
    assert action.raw is not None


def test_normalize_response_captures_thought_signature_as_provider_state() -> None:
    call = _FakeFunctionCall("get_order", {"order_id": 42})
    raw_sig = bytes([0]) + b"sig" + bytes([127])
    resp = _FakeResponse(
        function_calls=[call],
        parts=[_FakePart(function_call=call, thought_signature=raw_sig)],
    )

    action = _normalize_response(resp)

    assert action.provider_state == {
        THOUGHT_SIGNATURE_KEY: base64.b64encode(raw_sig).decode("ascii")
    }


def test_normalize_response_without_signature_has_no_provider_state() -> None:
    call = _FakeFunctionCall("get_order", {})
    resp = _FakeResponse(function_calls=[call], parts=[_FakePart(function_call=call)])
    assert _normalize_response(resp).provider_state is None


def test_normalize_response_rejects_parallel_tool_calls() -> None:
    resp = _FakeResponse(
        function_calls=[
            _FakeFunctionCall("get_order", {"order_id": 42}),
            _FakeFunctionCall("issue_refund", {"order_id": 42}),
        ]
    )

    with pytest.raises(ModelAdapterError, match="exactly one action"):
        _normalize_response(resp)


def test_normalize_response_final_answer() -> None:
    resp = _FakeResponse(text="Your refund has been issued.")

    action = _normalize_response(resp)

    assert action.kind is ActionKind.FINAL_ANSWER
    assert action.final_answer == "Your refund has been issued."
    assert action.raw is not None


def test_normalize_response_empty_raises() -> None:
    with pytest.raises(ModelAdapterError):
        _normalize_response(_FakeResponse())


# --- usage and cost (#196) ---


def test_usage_counts_thinking_as_output() -> None:
    """Gemini bills thinking tokens at the output rate, so they are output here."""
    raw = {
        "usage_metadata": {
            "prompt_token_count": 995,
            "candidates_token_count": 19,
            "thoughts_token_count": 152,
            "tool_use_prompt_token_count": None,
            "total_token_count": 1166,
        }
    }
    assert extract_usage(raw) == (995, 171)


def test_tool_use_prompt_tokens_count_as_input() -> None:
    raw = {
        "usage_metadata": {
            "prompt_token_count": 100,
            "candidates_token_count": 10,
            "tool_use_prompt_token_count": 40,
        }
    }
    assert extract_usage(raw) == (140, 10)


def test_a_response_with_no_usage_reads_as_none_rather_than_zero() -> None:
    assert extract_usage({}) is None
    assert extract_usage({"usage_metadata": None}) is None
    assert extract_usage({"usage_metadata": {"prompt_token_count": 5}}) is None
    assert extract_usage({"usage_metadata": {"prompt_token_count": True}}) is None


def test_cost_is_priced_from_the_recorded_usage() -> None:
    per_input, per_output = GEMINI_PRICING["gemini-2.5-flash"]
    raws = [
        {"usage_metadata": {"prompt_token_count": 1_000_000, "candidates_token_count": 0}},
        {
            "usage_metadata": {
                "prompt_token_count": 0,
                "candidates_token_count": 400_000,
                "thoughts_token_count": 600_000,
            }
        },
    ]
    assert gemini_cost("gemini-2.5-flash", raws) == pytest.approx(per_input + per_output)
    assert estimate_cost_usd("gemini", "gemini-2.5-flash", raws) == pytest.approx(
        per_input + per_output
    )


def test_an_unpriced_model_reports_null() -> None:
    raws = [{"usage_metadata": {"prompt_token_count": 10, "candidates_token_count": 1}}]
    assert gemini_cost("gemini-not-in-the-table", raws) is None
    assert not is_priced("gemini", "gemini-not-in-the-table")


def test_the_default_model_is_priced() -> None:
    """A capped suite on the default model would otherwise be refused outright."""
    raws = [{"usage_metadata": {"prompt_token_count": 1_000_000, "candidates_token_count": 0}}]
    assert is_priced("gemini", DEFAULT_GEMINI_MODEL)
    assert gemini_cost(DEFAULT_GEMINI_MODEL, raws) == pytest.approx(0.75)


def test_other_providers_usage_does_not_price_as_gemini() -> None:
    raws = [{"usage": {"input_tokens": 1_000_000, "output_tokens": 0}}]
    assert gemini_cost("gemini-2.5-flash", raws) is None


def test_every_priced_gemini_model_has_two_positive_numbers() -> None:
    for model, (per_input, per_output) in GEMINI_PRICING.items():
        assert per_input > 0 and per_output > 0, model


def test_the_dict_config_is_the_typed_config_the_sdk_would_build() -> None:
    """Checked against the real SDK models when google-genai happens to be
    installed; skipped otherwise, since the suite never requires it."""
    types = pytest.importorskip("google.genai.types")
    declarations = _tools_to_declarations(
        [ToolSpec(name="get_order", description="d", parameters={"type": "object"})]
    )
    typed = types.GenerateContentConfig(
        system_instruction="sys",
        tools=[types.Tool(function_declarations=declarations)],
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        temperature=0.2,
        seed=7,
    )
    built = _generate_config("sys", declarations, temperature=0.2, seed=7)
    assert types.GenerateContentConfig.model_validate(built) == typed
