"""Offline tests for the OpenAI adapter's pure logic (#160).

These never touch the network or need an API key, the same repo-wide rule the
Gemini and Anthropic tests follow. The conversion helpers return plain dicts
and ``_normalize_response`` is duck-typed, so the ``openai`` package is never
imported. Only the live ``chat.completions.create`` call needs the SDK, and
that path is verified by running a task with ``--provider openai``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from trace_harness.models import KNOWN_PROVIDERS, estimate_cost_usd, resolve_model_name
from trace_harness.models.base import ActionKind, Message, MessageRole, ModelAdapterError, ToolSpec
from trace_harness.models.openai import (
    DEFAULT_OPENAI_MODEL,
    OPENAI_PRICING,
    SYSTEM_FINGERPRINT_KEY,
    TOOL_CALL_ID_KEY,
    OpenAIModelAdapter,
    OpenAINotConfiguredError,
    _normalize_response,
    _tools_to_definitions,
    _transcript_to_messages,
    extract_usage,
)
from trace_harness.models.openai import estimate_cost_usd as openai_cost


@dataclass
class FakeFunction:
    name: str
    arguments: str


@dataclass
class FakeToolCall:
    function: FakeFunction
    id: str = "call_01"
    type: str = "function"


@dataclass
class FakeMessage:
    content: str | None = None
    tool_calls: list[FakeToolCall] | None = None


@dataclass
class FakeChoice:
    message: FakeMessage
    finish_reason: str = "stop"


@dataclass
class FakeResponse:
    choices: list[FakeChoice] = field(default_factory=list)
    usage: dict[str, int] | None = None
    system_fingerprint: str | None = None

    def model_dump(self, mode: str = "python") -> dict[str, Any]:
        dumped: dict[str, Any] = {"choices": len(self.choices)}
        if self.usage is not None:
            dumped["usage"] = self.usage
        if self.system_fingerprint is not None:
            dumped["system_fingerprint"] = self.system_fingerprint
        return dumped


def _text_response(text: str, **kw: Any) -> FakeResponse:
    return FakeResponse(choices=[FakeChoice(message=FakeMessage(content=text))], **kw)


def _call_response(
    name: str, arguments: str, *, call_id: str = "call_01", **kw: Any
) -> FakeResponse:
    call = FakeToolCall(function=FakeFunction(name=name, arguments=arguments), id=call_id)
    return FakeResponse(
        choices=[FakeChoice(message=FakeMessage(tool_calls=[call]), finish_reason="tool_calls")],
        **kw,
    )


# --- construction ---


def test_construction_without_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(OpenAINotConfiguredError):
        OpenAIModelAdapter()


def test_the_missing_key_message_says_what_to_do(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(OpenAINotConfiguredError) as caught:
        OpenAIModelAdapter()
    message = str(caught.value)
    assert "OPENAI_API_KEY" in message
    assert ".env" in message
    assert "fixture" in message


def test_construction_with_key_sets_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-used")
    adapter = OpenAIModelAdapter()
    assert adapter.name == "openai"
    assert adapter.model == DEFAULT_OPENAI_MODEL == "gpt-5"
    assert adapter.timeout_seconds == 120.0


def test_a_seed_is_kept_because_this_provider_actually_takes_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reason this adapter exists. Anthropic has no seed at all."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-used")
    assert OpenAIModelAdapter(seed=41).seed == 41


def test_the_provider_is_registered() -> None:
    assert "openai" in KNOWN_PROVIDERS
    assert resolve_model_name("openai", None, None) == DEFAULT_OPENAI_MODEL
    assert resolve_model_name("openai", "gpt-4.1", None) == "gpt-4.1"


# --- tool mapping ---


def test_tool_mapping_nests_the_schema_under_function() -> None:
    schema = {"type": "object", "properties": {"customer_name": {"type": "string"}}}
    tools = [ToolSpec(name="get_order", description="Look up an order", parameters=schema)]
    assert _tools_to_definitions(tools) == [
        {
            "type": "function",
            "function": {
                "name": "get_order",
                "description": "Look up an order",
                "parameters": schema,
            },
        }
    ]


def test_a_tool_with_no_parameters_still_gets_an_object_schema() -> None:
    definitions = _tools_to_definitions([ToolSpec(name="ping", description="d", parameters={})])
    assert definitions[0]["function"]["parameters"] == {"type": "object", "properties": {}}


def test_no_tools_maps_to_no_definitions() -> None:
    assert _tools_to_definitions([]) == []


# --- transcript mapping ---


def test_the_system_prompt_stays_a_message_here() -> None:
    """Unlike Gemini and Anthropic, where it is a separate parameter."""
    messages = _transcript_to_messages(
        [
            Message(role=MessageRole.SYSTEM, content="You are an agent."),
            Message(role=MessageRole.USER, content="Refund ORD-1."),
        ]
    )
    assert messages == [
        {"role": "system", "content": "You are an agent."},
        {"role": "user", "content": "Refund ORD-1."},
    ]


def test_an_assistant_tool_call_encodes_arguments_as_a_json_string() -> None:
    messages = _transcript_to_messages(
        [
            Message(
                role=MessageRole.ASSISTANT,
                content="",
                metadata={
                    "tool_call": {
                        "tool_name": "get_order",
                        "arguments": {"customer_name": "Casey"},
                    },
                    "provider_state": {TOOL_CALL_ID_KEY: "call_abc"},
                },
            )
        ]
    )
    call = messages[0]["tool_calls"][0]
    assert call["id"] == "call_abc"
    assert call["function"]["name"] == "get_order"
    assert call["function"]["arguments"] == '{"customer_name": "Casey"}'


def test_a_tool_message_quotes_the_id_of_the_call_it_answers() -> None:
    messages = _transcript_to_messages(
        [
            Message(
                role=MessageRole.ASSISTANT,
                content="",
                metadata={
                    "tool_call": {"tool_name": "get_order", "arguments": {}},
                    "provider_state": {TOOL_CALL_ID_KEY: "call_xyz"},
                },
            ),
            Message(
                role=MessageRole.TOOL,
                content="",
                metadata={"tool_name": "get_order", "result": {"amount_usd": 432.0}, "error": None},
            ),
        ]
    )
    assert messages[1] == {
        "role": "tool",
        "tool_call_id": "call_xyz",
        "content": '{"amount_usd": 432.0}',
    }


def test_a_tool_error_is_sent_as_the_content() -> None:
    """There is no error flag on an OpenAI tool message, so the text carries it."""
    messages = _transcript_to_messages(
        [
            Message(
                role=MessageRole.TOOL,
                content="no such order",
                metadata={"tool_name": "get_order", "result": None, "error": "no such order"},
            )
        ]
    )
    assert messages[0]["content"] == "no such order"


# --- normalization ---


def test_normalize_a_tool_call_parses_the_argument_string() -> None:
    action = _normalize_response(
        _call_response("issue_refund", '{"amount_usd": 432.0}', call_id="call_9")
    )
    assert action.kind is ActionKind.TOOL_CALL
    assert action.tool_call is not None
    assert action.tool_call.arguments == {"amount_usd": 432.0}
    assert action.provider_state == {TOOL_CALL_ID_KEY: "call_9"}


def test_arguments_that_are_not_valid_json_raise() -> None:
    """A call the runner cannot dispatch is worse than a turn that failed loudly."""
    with pytest.raises(ModelAdapterError, match="not valid JSON"):
        _normalize_response(_call_response("issue_refund", "{not json"))


def test_arguments_that_parse_to_a_non_object_raise() -> None:
    with pytest.raises(ModelAdapterError, match="not an object"):
        _normalize_response(_call_response("issue_refund", "[1, 2, 3]"))


def test_empty_arguments_map_to_an_empty_dict() -> None:
    action = _normalize_response(_call_response("list_orders", ""))
    assert action.tool_call is not None
    assert action.tool_call.arguments == {}


def test_normalize_rejects_parallel_tool_calls() -> None:
    calls = [
        FakeToolCall(function=FakeFunction("get_order", "{}"), id="a"),
        FakeToolCall(function=FakeFunction("issue_refund", "{}"), id="b"),
    ]
    response = FakeResponse(
        choices=[FakeChoice(message=FakeMessage(tool_calls=calls), finish_reason="tool_calls")]
    )
    with pytest.raises(ModelAdapterError, match="parallel tool calls"):
        _normalize_response(response)


def test_normalize_a_final_answer() -> None:
    action = _normalize_response(_text_response("All done."))
    assert action.kind is ActionKind.FINAL_ANSWER
    assert action.final_answer == "All done."


def test_normalize_an_empty_response_raises() -> None:
    with pytest.raises(ModelAdapterError, match="neither a tool call nor content"):
        _normalize_response(_text_response(""))


def test_no_choices_raises() -> None:
    with pytest.raises(ModelAdapterError, match="no choices"):
        _normalize_response(FakeResponse(choices=[]))


def test_a_content_filter_stop_raises() -> None:
    response = FakeResponse(
        choices=[FakeChoice(message=FakeMessage(content="x"), finish_reason="content_filter")]
    )
    with pytest.raises(ModelAdapterError, match="content filter"):
        _normalize_response(response)


def test_the_system_fingerprint_is_carried_so_a_seeded_rerun_can_be_checked() -> None:
    """A seeded run whose backend build moved is not a reproduction of the first."""
    action = _normalize_response(_text_response("done", system_fingerprint="fp_abc123"))
    assert action.provider_state == {SYSTEM_FINGERPRINT_KEY: "fp_abc123"}


def test_the_fingerprint_rides_alongside_the_tool_call_id() -> None:
    action = _normalize_response(
        _call_response("get_order", "{}", call_id="call_7", system_fingerprint="fp_9")
    )
    assert action.provider_state == {SYSTEM_FINGERPRINT_KEY: "fp_9", TOOL_CALL_ID_KEY: "call_7"}


# --- usage and cost ---


def test_usage_is_read_off_a_recorded_response() -> None:
    assert extract_usage({"usage": {"prompt_tokens": 1200, "completion_tokens": 300}}) == (
        1200,
        300,
    )


def test_a_response_with_no_usage_reads_as_none_rather_than_zero() -> None:
    assert extract_usage({}) is None
    assert extract_usage({"usage": {"prompt_tokens": "many"}}) is None


def test_cost_is_priced_from_the_recorded_usage() -> None:
    per_input, per_output = OPENAI_PRICING["gpt-5"]
    raws = [
        {"usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0}},
        {"usage": {"prompt_tokens": 0, "completion_tokens": 1_000_000}},
    ]
    assert openai_cost("gpt-5", raws) == pytest.approx(per_input + per_output)


def test_an_unpriced_model_reports_null_rather_than_zero() -> None:
    assert (
        openai_cost("gpt-unreleased", [{"usage": {"prompt_tokens": 1, "completion_tokens": 1}}])
        is None
    )


def test_the_dispatcher_routes_each_provider_to_its_own_pricer() -> None:
    openai_raws = [{"usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0}}]
    anthropic_raws = [{"usage": {"input_tokens": 1_000_000, "output_tokens": 0}}]
    assert estimate_cost_usd("openai", "gpt-5", openai_raws) == pytest.approx(1.25)
    from trace_harness.models.anthropic import ANTHROPIC_PRICING

    assert estimate_cost_usd("anthropic", "claude-sonnet-5", anthropic_raws) == pytest.approx(
        ANTHROPIC_PRICING["claude-sonnet-5"][0]
    )
    # Each reads its own field names, so one provider's usage does not price
    # under another's table.
    assert estimate_cost_usd("openai", "gpt-5", anthropic_raws) is None
    assert estimate_cost_usd("gemini", "gemini-3.6-flash", openai_raws) is None


def test_every_priced_model_has_two_positive_numbers() -> None:
    for model, (per_input, per_output) in OPENAI_PRICING.items():
        assert per_input > 0 and per_output > 0, model
