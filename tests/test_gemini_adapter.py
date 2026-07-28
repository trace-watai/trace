"""Offline tests for the Gemini adapter's pure logic.

These never touch the network or need an API key (repo-wide rule). The
conversion helpers return plain dicts and ``_normalize_response`` is duck-typed,
so a tiny fake response stands in for the real SDK object — no ``google-genai``
install required. Only the live ``generate_content`` call needs the SDK, and
that path is verified manually (``--provider gemini``), never here.
"""

from __future__ import annotations

import pytest

from trace_harness.models.base import ActionKind, Message, MessageRole, ModelAdapterError, ToolSpec
from trace_harness.models.gemini import (
    DEFAULT_GEMINI_MODEL,
    GeminiModelAdapter,
    GeminiNotConfiguredError,
    _normalize_response,
    _tools_to_declarations,
    _transcript_to_contents,
)

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
    assert contents[2]["role"] == "tool"
    assert contents[2]["parts"][0]["function_response"]["name"] == "get_order"


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


class _FakeResponse:
    """Stands in for google.genai's GenerateContentResponse."""

    def __init__(self, *, function_calls: list | None = None, text: str | None = None) -> None:
        self.function_calls = function_calls or []
        self.text = text

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
