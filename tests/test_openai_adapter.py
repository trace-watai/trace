"""Offline tests for the OpenAI adapter (#160).

These never touch the network or need an API key, the same repo-wide rule the
Gemini and Anthropic tests follow. The conversion helpers return plain dicts
and ``_normalize_response`` is duck-typed. The constructor and ``next_action``
run against a fake ``openai`` module from ``fake_provider_sdk``, so the request
the adapter builds is checked here without the package installed. Only the real
network call is left to a task run with ``--provider openai``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from conftest import VALID_TASK_PATH
from fake_provider_sdk import FakeSDK, FakeSDKError, block_sdk, install_openai
from trace_harness.models import (
    KNOWN_PROVIDERS,
    estimate_cost_usd,
    resolve_model_name,
    unsent_seed_metadata,
)
from trace_harness.models.base import ActionKind, Message, MessageRole, ModelAdapterError, ToolSpec
from trace_harness.models.openai import (
    DEFAULT_OPENAI_MODEL,
    FIXED_SAMPLING_MODELS,
    OPENAI_CACHED_INPUT_PRICING,
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
        if self.choices:
            dumped["finish_reason"] = self.choices[0].finish_reason
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


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch) -> FakeSDK:
    """A fake ``openai`` module and a key, enough to build the adapter."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-used")
    return install_openai(monkeypatch)


TOOLS = [ToolSpec(name="get_order", description="Look up an order", parameters={})]
TRANSCRIPT = [
    Message(role=MessageRole.SYSTEM, content="You are an agent."),
    Message(role=MessageRole.USER, content="Refund ORD-1."),
]


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


def test_construction_with_key_sets_defaults(sdk: FakeSDK) -> None:
    adapter = OpenAIModelAdapter()
    assert adapter.name == "openai"
    assert adapter.model == DEFAULT_OPENAI_MODEL == "gpt-5"
    assert adapter.timeout_seconds == 120.0


def test_a_seed_is_kept_because_this_provider_actually_takes_one(sdk: FakeSDK) -> None:
    """The reason this adapter exists. Anthropic has no seed at all."""
    assert OpenAIModelAdapter(seed=41).seed == 41
    # Sent, so nothing in the run's metadata says otherwise.
    assert unsent_seed_metadata("openai", 41) == {}
    assert unsent_seed_metadata("anthropic", 41) == {"seed_sent": False}
    assert unsent_seed_metadata("anthropic", None) == {}


def test_a_missing_sdk_fails_at_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-used")
    block_sdk(monkeypatch, "openai")
    with pytest.raises(OpenAINotConfiguredError, match=r'pip install -e "\.\[openai\]"'):
        OpenAIModelAdapter()


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


def _tool_call_turn(tool_name: str, provider_state: dict[str, Any] | None = None) -> Message:
    metadata: dict[str, Any] = {"tool_call": {"tool_name": tool_name, "arguments": {}}}
    if provider_state is not None:
        metadata["provider_state"] = provider_state
    return Message(role=MessageRole.ASSISTANT, content="", metadata=metadata)


def test_a_tool_error_is_sent_as_the_content() -> None:
    """There is no error flag on an OpenAI tool message, so the text carries it."""
    messages = _transcript_to_messages(
        [
            _tool_call_turn("get_order", {TOOL_CALL_ID_KEY: "call_1"}),
            Message(
                role=MessageRole.TOOL,
                content="no such order",
                metadata={"tool_name": "get_order", "result": None, "error": "no such order"},
            ),
        ]
    )
    assert messages[1]["content"] == "no such order"


def test_a_tool_call_without_an_id_gets_a_stable_one_its_result_quotes() -> None:
    """A scripted prefix before a live continuation has no OpenAI ids."""
    transcript = [
        Message(role=MessageRole.USER, content="Refund ORD-1."),
        _tool_call_turn("get_order"),
        Message(role=MessageRole.TOOL, content="", metadata={"result": {"ok": True}}),
    ]
    messages = _transcript_to_messages(transcript)
    assert messages[1]["tool_calls"][0]["id"] == messages[2]["tool_call_id"] == "call_trace_1"
    assert _transcript_to_messages(transcript) == messages


def test_a_tool_result_with_no_call_before_it_is_an_adapter_error() -> None:
    with pytest.raises(ModelAdapterError, match="no tool call before it"):
        _transcript_to_messages(
            [Message(role=MessageRole.TOOL, content="", metadata={"result": "orphan"})]
        )


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
    with pytest.raises(ModelAdapterError, match="parallel tool calls") as caught:
        _normalize_response(response)
    # The provider billed for it, so the runner has to be able to record it.
    assert caught.value.raw == response.model_dump()


def test_a_tool_call_without_an_id_is_an_adapter_error() -> None:
    with pytest.raises(ModelAdapterError, match="no id"):
        _normalize_response(_call_response("get_order", "{}", call_id=""))


def test_a_turn_cut_off_at_the_length_limit_is_an_error_that_keeps_the_response() -> None:
    response = FakeResponse(
        choices=[FakeChoice(message=FakeMessage(content="The refund is"), finish_reason="length")],
        usage={"prompt_tokens": 10, "completion_tokens": 100},
    )
    with pytest.raises(ModelAdapterError, match="length") as caught:
        _normalize_response(response)
    assert caught.value.raw == response.model_dump()


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


#: The published standard-tier prices, copied by hand from
#: https://developers.openai.com/api/docs/pricing on 2026-09-24, as (input,
#: output) and cached input, USD per million tokens.
PUBLISHED_PRICES = {
    "gpt-5": (1.25, 10.0),
    "gpt-5-mini": (0.25, 2.0),
    "gpt-4.1": (2.0, 8.0),
    "gpt-4.1-mini": (0.4, 1.6),
}
PUBLISHED_CACHED_INPUT_PRICES = {
    "gpt-5": 0.125,
    "gpt-5-mini": 0.025,
    "gpt-4.1": 0.5,
    "gpt-4.1-mini": 0.1,
}


def test_the_price_tables_match_the_published_prices() -> None:
    assert OPENAI_PRICING == PUBLISHED_PRICES
    assert OPENAI_CACHED_INPUT_PRICING == PUBLISHED_CACHED_INPUT_PRICES


def test_cached_prompt_tokens_are_priced_at_the_cached_rate() -> None:
    """prompt_tokens includes the cached ones, so they are moved from the full
    input rate to the cached rate rather than added."""
    per_input, _ = OPENAI_PRICING["gpt-5"]
    raw = {
        "usage": {
            "prompt_tokens": 1_000_000,
            "completion_tokens": 0,
            "prompt_tokens_details": {"cached_tokens": 800_000},
        }
    }
    assert openai_cost("gpt-5", [raw]) == pytest.approx(
        0.2 * per_input + 0.8 * OPENAI_CACHED_INPUT_PRICING["gpt-5"]
    )


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
    assert estimate_cost_usd("openai", "gpt-5", openai_raws) == pytest.approx(
        OPENAI_PRICING["gpt-5"][0]
    )
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


# --- the request next_action sends, on a fake SDK ---


def test_the_request_carries_the_model_messages_and_tools(sdk: FakeSDK) -> None:
    sdk.script(_text_response("All done."))
    OpenAIModelAdapter(timeout_seconds=30.0).next_action(TRANSCRIPT, TOOLS)
    (request,) = sdk.requests
    assert request["model"] == "gpt-5"
    assert request["messages"] == _transcript_to_messages(TRANSCRIPT)
    assert request["tools"] == _tools_to_definitions(TOOLS)
    assert sdk.client_kwargs == {"api_key": "test-key-not-used", "timeout": 30.0}


def test_the_seed_is_sent(sdk: FakeSDK) -> None:
    """The one thing this provider has that Anthropic does not."""
    sdk.script(_text_response("All done."), _text_response("All done."))
    OpenAIModelAdapter(seed=41).next_action(TRANSCRIPT, TOOLS)
    OpenAIModelAdapter().next_action(TRANSCRIPT, TOOLS)
    seeded, unseeded = sdk.requests
    assert seeded["seed"] == 41
    assert "seed" not in unseeded


def test_parallel_tool_calls_are_switched_off_whenever_tools_are_sent(sdk: FakeSDK) -> None:
    sdk.script(_text_response("All done."), _text_response("All done."))
    adapter = OpenAIModelAdapter()
    adapter.next_action(TRANSCRIPT, TOOLS)
    adapter.next_action(TRANSCRIPT, [])
    with_tools, without_tools = sdk.requests
    assert with_tools["parallel_tool_calls"] is False
    assert "parallel_tool_calls" not in without_tools
    assert "tools" not in without_tools


def test_a_temperature_is_sent_to_a_model_that_accepts_it(sdk: FakeSDK) -> None:
    sdk.script(_text_response("All done."), _text_response("All done."))
    OpenAIModelAdapter(model="gpt-4.1", temperature=0.2).next_action(TRANSCRIPT, TOOLS)
    OpenAIModelAdapter(model="gpt-4.1").next_action(TRANSCRIPT, TOOLS)
    warm, default = sdk.requests
    assert warm["temperature"] == 0.2
    assert "temperature" not in default


@pytest.mark.parametrize("model", ["gpt-5", "gpt-5-mini", "gpt-5-2025-08-07", "o3", "gpt-6-sol"])
def test_a_temperature_for_a_reasoning_model_fails_at_construction(
    sdk: FakeSDK, model: str
) -> None:
    """At its default reasoning effort, a reasoning model rejects any
    non-default temperature, so the run would be nothing but model errors."""
    with pytest.raises(OpenAINotConfiguredError, match="temperature") as caught:
        OpenAIModelAdapter(model=model, temperature=0.2)
    assert model in str(caught.value)
    assert sdk.requests == []
    OpenAIModelAdapter(model=model)


def test_the_gpt_5_family_is_in_the_fixed_sampling_set() -> None:
    assert {"gpt-5", "gpt-5-mini", "gpt-5-nano"} <= FIXED_SAMPLING_MODELS
    assert "gpt-4.1" not in FIXED_SAMPLING_MODELS


def test_an_sdk_error_becomes_a_model_error(sdk: FakeSDK) -> None:
    sdk.script(FakeSDKError("rate limited"))
    with pytest.raises(ModelAdapterError, match="OpenAI API call failed: rate limited"):
        OpenAIModelAdapter().next_action(TRANSCRIPT, TOOLS)


def test_the_cli_refuses_a_temperature_gpt_5_rejects(
    tmp_path, sdk: FakeSDK, capsys: pytest.CaptureFixture[str]
) -> None:
    from trace_harness.cli import main

    argv = ["--runs-dir", str(tmp_path / "runs"), "run-fixture", str(VALID_TASK_PATH)]
    assert main([*argv, "--provider", "openai", "--temperature", "0.2"]) == 2
    assert "gpt-5 is a reasoning model" in capsys.readouterr().err
    assert sdk.requests == []


def test_a_billed_response_that_ends_the_run_is_still_priced(tmp_path, sdk: FakeSDK) -> None:
    """Two tool calls end the run as a model error. The turn was billed, so the
    batch entry is priced from it rather than reporting less than it spent."""
    from trace_harness.runner.batch import BatchRunner
    from trace_harness.runner.suite import AgentConfig, SuiteSpec
    from trace_harness.tracing.artifact_store import ArtifactStore

    calls = [
        FakeToolCall(function=FakeFunction("get_order", "{}"), id="a"),
        FakeToolCall(function=FakeFunction("issue_refund", "{}"), id="b"),
    ]
    sdk.script(
        FakeResponse(
            choices=[FakeChoice(message=FakeMessage(tool_calls=calls), finish_reason="tool_calls")],
            usage={"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000},
        )
    )
    store = ArtifactStore(tmp_path / "runs")
    suite = SuiteSpec(
        suite_id="openai_billed_error",
        tasks=[str(VALID_TASK_PATH)],
        agent_configs=[AgentConfig(label="gpt", provider="openai", seed=3)],
    )
    (entry,) = BatchRunner(store).run(suite).entries
    assert entry.termination_reason == "model_error"
    per_input, per_output = OPENAI_PRICING["gpt-5"]
    assert entry.cost_usd == pytest.approx(per_input + per_output)
    config = json.loads(
        (store.runs_dir / entry.run_id / "run_config.json").read_text(encoding="utf-8")
    )
    assert "seed_sent" not in config["metadata"]
