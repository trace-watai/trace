"""Offline tests for the Anthropic adapter (#160).

These never touch the network or need an API key, same repo-wide rule the
Gemini tests follow. The conversion helpers return plain dicts and
``_normalize_response`` is duck-typed, so a small fake response stands in for
the SDK object. The constructor and ``next_action`` run against a fake
``anthropic`` module from ``fake_provider_sdk``, so the request the adapter
builds is checked here without the package installed. Only the real network
call is left to a task run with ``--provider anthropic``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from conftest import VALID_TASK_PATH
from fake_provider_sdk import FakeSDK, FakeSDKError, block_sdk, install_anthropic
from trace_harness.cli import main
from trace_harness.models import KNOWN_PROVIDERS, estimate_cost_usd, resolve_model_name
from trace_harness.models.anthropic import (
    ANTHROPIC_PRICING,
    DEFAULT_ANTHROPIC_MODEL,
    DEFAULT_MAX_TOKENS,
    FIXED_SAMPLING_MODELS,
    THINKING_BLOCKS_KEY,
    TOOL_USE_ID_KEY,
    AnthropicModelAdapter,
    AnthropicNotConfiguredError,
    _normalize_response,
    _tools_to_definitions,
    _transcript_to_messages,
    extract_usage,
)
from trace_harness.models.anthropic import estimate_cost_usd as anthropic_cost
from trace_harness.models.base import ActionKind, Message, MessageRole, ModelAdapterError, ToolSpec

# --- fakes standing in for the SDK's response objects ---


@dataclass
class FakeText:
    text: str
    type: str = "text"


@dataclass
class FakeToolUse:
    name: str
    input: dict[str, Any]
    id: str = "toolu_01"
    type: str = "tool_use"


@dataclass
class FakeThinking:
    signature: str
    thinking: str = ""
    type: str = "thinking"


@dataclass
class FakeRedactedThinking:
    data: str
    type: str = "redacted_thinking"


@dataclass
class FakeResponse:
    content: list[Any] = field(default_factory=list)
    stop_reason: str | None = "end_turn"
    usage: dict[str, int] | None = None

    def model_dump(self, mode: str = "python") -> dict[str, Any]:
        dumped: dict[str, Any] = {
            "stop_reason": self.stop_reason,
            "content": [block.__dict__ for block in self.content],
        }
        if self.usage is not None:
            dumped["usage"] = self.usage
        return dumped


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch) -> FakeSDK:
    """A fake ``anthropic`` module and a key, enough to build the adapter."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    return install_anthropic(monkeypatch)


TOOLS = [ToolSpec(name="get_order", description="Look up an order", parameters={})]
TRANSCRIPT = [
    Message(role=MessageRole.SYSTEM, content="You are an agent."),
    Message(role=MessageRole.USER, content="Refund ORD-1."),
]


# --- construction ---


def test_construction_without_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(AnthropicNotConfiguredError):
        AnthropicModelAdapter()


def test_the_missing_key_message_says_what_to_do(monkeypatch: pytest.MonkeyPatch) -> None:
    """Failing fast is only useful when the message is actionable."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(AnthropicNotConfiguredError) as caught:
        AnthropicModelAdapter()
    message = str(caught.value)
    assert "ANTHROPIC_API_KEY" in message
    assert ".env" in message
    assert "fixture" in message


def test_construction_with_key_sets_defaults(sdk: FakeSDK) -> None:
    adapter = AnthropicModelAdapter()
    assert adapter.name == "anthropic"
    assert adapter.model == DEFAULT_ANTHROPIC_MODEL == "claude-sonnet-5"
    assert adapter.max_tokens == DEFAULT_MAX_TOKENS == 16000
    assert adapter.timeout_seconds == 120.0


def test_a_seed_is_recorded_even_though_it_is_never_sent(sdk: FakeSDK) -> None:
    """The Messages API has no seed. run_config.json still records what was asked."""
    assert AnthropicModelAdapter(seed=7).seed == 7


def test_a_missing_sdk_fails_at_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the key set but no package, the adapter refuses to be built, so the
    CLI prints the install line and no run starts."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    block_sdk(monkeypatch, "anthropic")
    with pytest.raises(AnthropicNotConfiguredError, match=r'pip install -e "\.\[anthropic\]"'):
        AnthropicModelAdapter()


def test_run_fixture_exits_2_with_the_install_line_when_the_sdk_is_missing(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    block_sdk(monkeypatch, "anthropic")
    argv = ["--runs-dir", str(tmp_path / "runs"), "run-fixture", str(VALID_TASK_PATH)]
    assert main([*argv, "--provider", "anthropic"]) == 2
    assert "'anthropic' package is not installed" in capsys.readouterr().err
    assert not (tmp_path / "runs").exists() or not any((tmp_path / "runs").iterdir())


def test_run_fixture_exits_2_with_instructions_when_the_key_is_missing(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI's handler for a provider that cannot be used, with no traceback."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    argv = ["--runs-dir", str(tmp_path / "runs"), "run-fixture", str(VALID_TASK_PATH)]
    assert main([*argv, "--provider", "anthropic"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("error: ANTHROPIC_API_KEY is not set")
    assert "Traceback" not in err


def test_the_provider_is_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    assert "anthropic" in KNOWN_PROVIDERS
    assert resolve_model_name("anthropic", None, None) == DEFAULT_ANTHROPIC_MODEL
    assert resolve_model_name("anthropic", "claude-opus-5", None) == "claude-opus-5"


# --- tool mapping ---


def test_tool_mapping_uses_the_spec_json_schema() -> None:
    schema = {"type": "object", "properties": {"customer_name": {"type": "string"}}}
    tools = [ToolSpec(name="get_order", description="Look up an order", parameters=schema)]
    assert _tools_to_definitions(tools) == [
        {"name": "get_order", "description": "Look up an order", "input_schema": schema}
    ]


def test_a_tool_with_no_parameters_still_gets_an_object_schema() -> None:
    """The API rejects a bare empty schema, so an argument-free tool needs one."""
    definitions = _tools_to_definitions([ToolSpec(name="ping", description="d", parameters={})])
    assert definitions[0]["input_schema"] == {"type": "object", "properties": {}}


def test_no_tools_maps_to_no_definitions() -> None:
    assert _tools_to_definitions([]) == []


# --- transcript mapping ---


def test_system_messages_leave_the_message_list() -> None:
    system, messages = _transcript_to_messages(
        [
            Message(role=MessageRole.SYSTEM, content="You are an agent."),
            Message(role=MessageRole.SYSTEM, content="Follow the policy."),
            Message(role=MessageRole.USER, content="Refund ORD-1."),
        ]
    )
    assert system == "You are an agent.\nFollow the policy."
    assert messages == [{"role": "user", "content": [{"type": "text", "text": "Refund ORD-1."}]}]


def test_no_system_message_maps_to_none() -> None:
    system, messages = _transcript_to_messages([Message(role=MessageRole.USER, content="hi")])
    assert system is None
    assert len(messages) == 1


def test_an_assistant_tool_call_maps_to_a_tool_use_block() -> None:
    _, messages = _transcript_to_messages(
        [
            Message(
                role=MessageRole.ASSISTANT,
                content="",
                metadata={
                    "tool_call": {
                        "tool_name": "get_order",
                        "arguments": {"customer_name": "Casey"},
                    },
                    "provider_state": {TOOL_USE_ID_KEY: "toolu_abc"},
                },
            )
        ]
    )
    assert messages == [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_abc",
                    "name": "get_order",
                    "input": {"customer_name": "Casey"},
                }
            ],
        }
    ]


def test_an_assistant_text_turn_maps_to_a_text_block() -> None:
    _, messages = _transcript_to_messages(
        [Message(role=MessageRole.ASSISTANT, content="Refund issued.", metadata={})]
    )
    assert messages == [
        {"role": "assistant", "content": [{"type": "text", "text": "Refund issued."}]}
    ]


def test_a_tool_result_quotes_the_id_of_the_call_it_answers() -> None:
    """Pairing by id is the one real difference from the Gemini mapping."""
    _, messages = _transcript_to_messages(
        [
            Message(
                role=MessageRole.ASSISTANT,
                content="",
                metadata={
                    "tool_call": {"tool_name": "get_order", "arguments": {}},
                    "provider_state": {TOOL_USE_ID_KEY: "toolu_xyz"},
                },
            ),
            Message(
                role=MessageRole.TOOL,
                content="",
                metadata={"tool_name": "get_order", "result": {"amount_usd": 432.0}, "error": None},
            ),
        ]
    )
    result = messages[1]["content"][0]
    assert result["type"] == "tool_result"
    assert result["tool_use_id"] == "toolu_xyz"
    assert result["is_error"] is False
    assert result["content"] == '{"amount_usd": 432.0}'


def _tool_call_turn(tool_name: str, provider_state: dict[str, Any] | None = None) -> Message:
    metadata: dict[str, Any] = {"tool_call": {"tool_name": tool_name, "arguments": {}}}
    if provider_state is not None:
        metadata["provider_state"] = provider_state
    return Message(role=MessageRole.ASSISTANT, content="", metadata=metadata)


def test_a_tool_error_is_sent_as_the_result_with_is_error_set() -> None:
    """A failed tool call is behavior the verifier judges. It must reach the model."""
    _, messages = _transcript_to_messages(
        [
            _tool_call_turn("get_order", {TOOL_USE_ID_KEY: "toolu_1"}),
            Message(
                role=MessageRole.TOOL,
                content="no such order",
                metadata={"tool_name": "get_order", "result": None, "error": "no such order"},
            ),
        ]
    )
    result = messages[1]["content"][0]
    assert result["is_error"] is True
    assert result["content"] == "no such order"


def test_a_string_tool_result_is_not_json_wrapped() -> None:
    _, messages = _transcript_to_messages(
        [
            _tool_call_turn("search_docs", {TOOL_USE_ID_KEY: "toolu_1"}),
            Message(
                role=MessageRole.TOOL,
                content="",
                metadata={"tool_name": "search_docs", "result": "policy text", "error": None},
            ),
        ]
    )
    assert messages[1]["content"][0]["content"] == "policy text"


def test_a_tool_call_without_an_id_gets_a_stable_one_its_result_quotes() -> None:
    """A scripted prefix, as the branch stage replays before a live
    continuation, has no Anthropic ids. An empty id is a 400, so each such turn
    gets one from its position, the same on every request."""
    transcript = [
        Message(role=MessageRole.USER, content="Refund ORD-1."),
        _tool_call_turn("get_order"),
        Message(role=MessageRole.TOOL, content="", metadata={"result": {"ok": True}}),
        _tool_call_turn("issue_refund", {"thought_signature": "from another provider"}),
        Message(role=MessageRole.TOOL, content="", metadata={"result": {"ok": True}}),
    ]
    _, messages = _transcript_to_messages(transcript)
    first_call, first_result = messages[1]["content"][0], messages[2]["content"][0]
    second_call, second_result = messages[3]["content"][0], messages[4]["content"][0]
    assert first_call["id"] == first_result["tool_use_id"] == "toolu_trace_1"
    assert second_call["id"] == second_result["tool_use_id"] == "toolu_trace_3"
    assert _transcript_to_messages(transcript)[1] == messages


def test_a_tool_result_with_no_call_before_it_is_an_adapter_error() -> None:
    with pytest.raises(ModelAdapterError, match="no tool call before it"):
        _transcript_to_messages(
            [Message(role=MessageRole.TOOL, content="", metadata={"result": "orphan"})]
        )


def test_thinking_blocks_go_back_unmodified_in_front_of_the_tool_use() -> None:
    thinking = [
        {"type": "thinking", "thinking": "", "signature": "sig-1"},
        {"type": "redacted_thinking", "data": "opaque"},
    ]
    state = {TOOL_USE_ID_KEY: "toolu_1", THINKING_BLOCKS_KEY: thinking}
    _, messages = _transcript_to_messages([_tool_call_turn("get_order", state)])
    assert messages[0]["content"] == [
        *thinking,
        {"type": "tool_use", "id": "toolu_1", "name": "get_order", "input": {}},
    ]


# --- normalization ---


def test_normalize_a_tool_call() -> None:
    response = FakeResponse(
        content=[FakeToolUse(name="issue_refund", input={"amount_usd": 432.0}, id="toolu_9")],
        stop_reason="tool_use",
    )
    action = _normalize_response(response)
    assert action.kind is ActionKind.TOOL_CALL
    assert action.tool_call is not None
    assert action.tool_call.tool_name == "issue_refund"
    assert action.tool_call.arguments == {"amount_usd": 432.0}
    assert action.provider_state == {TOOL_USE_ID_KEY: "toolu_9"}
    assert action.raw is not None


def test_thinking_blocks_are_kept_in_order_and_field_for_field() -> None:
    """Sonnet 5 thinks by default, and a tool-use turn sent back without its
    thinking, or with it edited or reordered, is rejected."""
    response = FakeResponse(
        content=[
            FakeThinking(signature="sig-1"),
            FakeRedactedThinking(data="opaque"),
            FakeThinking(signature="sig-2", thinking="summary text"),
            FakeText(text="Looking this up."),
            FakeToolUse(name="get_order", input={}, id="toolu_2"),
        ],
        stop_reason="tool_use",
    )
    action = _normalize_response(response)
    assert action.provider_state == {
        TOOL_USE_ID_KEY: "toolu_2",
        THINKING_BLOCKS_KEY: [
            {"type": "thinking", "thinking": "", "signature": "sig-1"},
            {"type": "redacted_thinking", "data": "opaque"},
            {"type": "thinking", "thinking": "summary text", "signature": "sig-2"},
        ],
    }
    assert action.reasoning == "Looking this up."


def test_a_tool_use_block_without_an_id_is_an_adapter_error() -> None:
    response = FakeResponse(content=[FakeToolUse(name="get_order", input={}, id="")])
    with pytest.raises(ModelAdapterError, match="no id"):
        _normalize_response(response)


@pytest.mark.parametrize("stop_reason", ["max_tokens", "model_context_window_exceeded"])
def test_a_truncated_turn_is_an_error_that_keeps_the_billed_response(stop_reason: str) -> None:
    """Thinking counts toward max_tokens. Text cut off there is not a final
    answer, and a half-written tool call is not a call."""
    response = FakeResponse(
        content=[FakeText(text="The refund is")],
        stop_reason=stop_reason,
        usage={"input_tokens": 10, "output_tokens": 16000},
    )
    with pytest.raises(ModelAdapterError, match=stop_reason) as caught:
        _normalize_response(response)
    assert caught.value.raw == response.model_dump()


def test_text_beside_a_tool_call_is_kept_as_reasoning() -> None:
    response = FakeResponse(
        content=[FakeText(text="Looking this up."), FakeToolUse(name="get_order", input={})],
        stop_reason="tool_use",
    )
    assert _normalize_response(response).reasoning == "Looking this up."


def test_normalize_rejects_parallel_tool_calls() -> None:
    """Two actions recorded as one step would make attribution point at a step
    that never happened, so this is an error rather than a silent drop."""
    response = FakeResponse(
        content=[
            FakeToolUse(name="get_order", input={}),
            FakeToolUse(name="issue_refund", input={}),
        ],
        stop_reason="tool_use",
    )
    with pytest.raises(ModelAdapterError, match="parallel tool calls") as caught:
        _normalize_response(response)
    # The provider billed for it, so the runner has to be able to record it.
    assert caught.value.raw == response.model_dump()


def test_normalize_a_final_answer() -> None:
    action = _normalize_response(FakeResponse(content=[FakeText(text="All done.")]))
    assert action.kind is ActionKind.FINAL_ANSWER
    assert action.final_answer == "All done."


def test_normalize_joins_multiple_text_blocks() -> None:
    action = _normalize_response(FakeResponse(content=[FakeText(text="a"), FakeText(text="b")]))
    assert action.final_answer == "a\nb"


def test_normalize_an_empty_response_raises() -> None:
    with pytest.raises(ModelAdapterError, match="neither a tool call nor text"):
        _normalize_response(FakeResponse(content=[]))


def test_normalize_a_refusal_raises() -> None:
    response = FakeResponse(
        content=[FakeText(text="I can't help with that.")], stop_reason="refusal"
    )
    with pytest.raises(ModelAdapterError, match="refusal"):
        _normalize_response(response)


def test_the_raw_response_is_carried_so_the_trace_records_it() -> None:
    """The runner writes action.raw as a model_response event before the
    normalized action, which is what puts the provider's own bytes in the trace."""
    response = FakeResponse(
        content=[FakeText(text="done")], usage={"input_tokens": 5, "output_tokens": 2}
    )
    assert _normalize_response(response).raw == {
        "stop_reason": "end_turn",
        "content": [{"text": "done", "type": "text"}],
        "usage": {"input_tokens": 5, "output_tokens": 2},
    }


# --- usage and cost ---


def test_usage_is_read_off_a_recorded_response() -> None:
    assert extract_usage({"usage": {"input_tokens": 1200, "output_tokens": 300}}) == (1200, 300)


def test_a_response_with_no_usage_reads_as_none_rather_than_zero() -> None:
    """None and (0, 0) mean different things. A fixture or a cassette has no usage."""
    assert extract_usage({}) is None
    assert extract_usage({"usage": {"input_tokens": "many"}}) is None


#: The published prices, copied by hand from
#: https://platform.claude.com/docs/en/about-claude/pricing on 2026-09-24, as
#: (input, output) USD per million tokens. A change to the table in code has to
#: be a change here too, checked against that page on the day it is made.
PUBLISHED_PRICES = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
}


def test_the_price_table_matches_the_published_prices() -> None:
    assert ANTHROPIC_PRICING == PUBLISHED_PRICES


def test_cache_tokens_are_priced_at_their_own_rates() -> None:
    """Cache reads and writes sit beside input_tokens, and during tool use the
    history is cached without being asked, so leaving them out undercounts."""
    per_input, _ = ANTHROPIC_PRICING["claude-sonnet-5"]
    raw = {
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 1_000_000,
            "cache_creation_input_tokens": 3_000_000,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 2_000_000,
                "ephemeral_1h_input_tokens": 1_000_000,
            },
        }
    }
    assert anthropic_cost("claude-sonnet-5", [raw]) == pytest.approx(
        per_input * (0.1 + 2 * 1.25 + 2.0)
    )
    # Without the split, every write is priced as a 5-minute one.
    del raw["usage"]["cache_creation"]
    assert anthropic_cost("claude-sonnet-5", [raw]) == pytest.approx(per_input * (0.1 + 3 * 1.25))


def test_cache_reads_on_opus_5_5_and_fable_5_1_use_their_own_multipliers() -> None:
    raw = {"usage": {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 10**6}}
    assert anthropic_cost("claude-opus-5-5", [raw]) == pytest.approx(0.20)
    assert anthropic_cost("claude-fable-5-1", [raw]) == pytest.approx(0.25)
    assert anthropic_cost("claude-opus-5", [raw]) == pytest.approx(0.50)


def test_cost_is_priced_from_the_recorded_usage() -> None:
    per_input, per_output = ANTHROPIC_PRICING["claude-sonnet-5"]
    raws = [
        {"usage": {"input_tokens": 1_000_000, "output_tokens": 0}},
        {"usage": {"input_tokens": 0, "output_tokens": 1_000_000}},
    ]
    assert anthropic_cost("claude-sonnet-5", raws) == pytest.approx(per_input + per_output)


def test_an_unpriced_model_reports_null_rather_than_zero() -> None:
    """A run that cost money and reports zero is worse than one reporting nothing."""
    assert (
        anthropic_cost(
            "claude-not-in-the-table", [{"usage": {"input_tokens": 1, "output_tokens": 1}}]
        )
        is None
    )


def test_no_usage_anywhere_reports_null() -> None:
    assert anthropic_cost("claude-sonnet-5", [{}, {}]) is None


def test_the_dispatcher_prices_anthropic_and_leaves_other_providers_null() -> None:
    raws = [{"usage": {"input_tokens": 1_000_000, "output_tokens": 0}}]
    assert estimate_cost_usd("anthropic", "claude-sonnet-5", raws) == pytest.approx(
        ANTHROPIC_PRICING["claude-sonnet-5"][0]
    )
    # Gemini has no price table yet, so its runs keep reporting null.
    assert estimate_cost_usd("gemini", "gemini-3.6-flash", raws) is None


def test_every_priced_model_has_two_positive_numbers() -> None:
    for model, (per_input, per_output) in ANTHROPIC_PRICING.items():
        assert per_input > 0 and per_output > 0, model


# --- the suite and cost path, with the live call stubbed out ---


class _StubAdapter:
    """An adapter that answers like Anthropic would, without the SDK or a key.

    Stands in at the ``create_model_adapter`` seam so the suite path, the
    persisted run config and the cost roll-up can be checked offline. The
    actions carry a ``raw`` with usage, which is exactly what the real adapter
    puts there and what the runner writes as a ``model_response`` event.
    """

    name = "anthropic"

    def __init__(self) -> None:
        self.turns = 0

    def next_action(self, transcript: list[Message], tools: list[ToolSpec]):  # noqa: ARG002
        from trace_harness.models.base import AgentAction

        self.turns += 1
        return AgentAction(
            kind=ActionKind.FINAL_ANSWER,
            final_answer="Nothing to refund here.",
            raw={
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1_000_000, "output_tokens": 1_000_000},
            },
        )


def _anthropic_suite(task_path: str):
    from trace_harness.runner.suite import AgentConfig, SuiteSpec

    return SuiteSpec(
        suite_id="anthropic_probe",
        tasks=[task_path],
        agent_configs=[
            AgentConfig(
                label="claude",
                provider="anthropic",
                # Haiku 4.5 takes a temperature; Sonnet 5 would be refused.
                model="claude-haiku-4-5",
                temperature=0.2,
                seed=41,
                max_steps=8,
                timeout_seconds=30.0,
            )
        ],
    )


@pytest.fixture
def stubbed_suite_run(tmp_path, monkeypatch: pytest.MonkeyPatch):
    from conftest import VALID_TASK_PATH
    from trace_harness.runner import pipeline
    from trace_harness.runner.batch import BatchRunner
    from trace_harness.tracing.artifact_store import ArtifactStore

    def fake_create(provider: str, **kwargs):
        assert provider == "anthropic"
        return _StubAdapter()

    monkeypatch.setattr(pipeline, "create_model_adapter", fake_create)
    store = ArtifactStore(tmp_path / "runs")
    summary = BatchRunner(store).run(_anthropic_suite(str(VALID_TASK_PATH)))
    return store, summary


def test_a_suite_agent_config_persists_the_provider_knobs(stubbed_suite_run) -> None:
    """run_config.json is the record of what was asked for, including the seed
    this provider never receives, and it says that seed was never sent."""
    store, summary = stubbed_suite_run
    entry = summary.entries[0]
    config = json.loads(
        (store.runs_dir / entry.run_id / "run_config.json").read_text(encoding="utf-8")
    )
    assert config["provider"] == "anthropic"
    assert config["model"] == "claude-haiku-4-5"
    assert config["temperature"] == 0.2
    assert config["seed"] == 41
    assert config["metadata"]["seed_sent"] is False
    assert config["max_steps"] == 8
    assert config["timeout_seconds"] == 30.0


def test_the_provider_response_precedes_the_normalized_action(stubbed_suite_run) -> None:
    """A reader has to be able to see what the vendor said before what we made
    of it, or the normalization is unfalsifiable."""
    store, summary = stubbed_suite_run
    lines = (
        (store.runs_dir / summary.entries[0].run_id / "trace.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    kinds = [json.loads(line)["event_type"] for line in lines if line.strip()]
    assert "model_response" in kinds
    assert kinds.index("model_response") < kinds.index("model_action")


def test_cost_usd_is_a_number_rather_than_null(stubbed_suite_run) -> None:
    """The whole point of usage extraction: a live entry stops reporting null."""
    _, summary = stubbed_suite_run
    per_input, per_output = ANTHROPIC_PRICING["claude-haiku-4-5"]
    entry = summary.entries[0]
    assert entry.cost_usd == pytest.approx(per_input + per_output)
    assert summary.aggregates.known_cost_usd == pytest.approx(per_input + per_output)


# --- the request next_action sends, on a fake SDK ---


def _final(text: str = "All done.") -> FakeResponse:
    return FakeResponse(content=[FakeText(text=text)])


def test_the_request_carries_the_model_max_tokens_system_and_tools(sdk: FakeSDK) -> None:
    sdk.script(_final())
    AnthropicModelAdapter(timeout_seconds=30.0).next_action(TRANSCRIPT, TOOLS)
    (request,) = sdk.requests
    assert request["model"] == "claude-sonnet-5"
    assert request["max_tokens"] == DEFAULT_MAX_TOKENS
    assert request["system"] == "You are an agent."
    assert request["tools"] == _tools_to_definitions(TOOLS)
    assert sdk.client_kwargs == {"api_key": "test-key-not-used", "timeout": 30.0}


def test_parallel_tool_use_is_switched_off_whenever_tools_are_sent(sdk: FakeSDK) -> None:
    """disable_parallel_tool_use lives inside tool_choice, and "auto" keeps a
    plain-text final answer possible (parallel-tool-use docs)."""
    sdk.script(_final(), _final())
    adapter = AnthropicModelAdapter()
    adapter.next_action(TRANSCRIPT, TOOLS)
    adapter.next_action(TRANSCRIPT, [])
    with_tools, without_tools = sdk.requests
    assert with_tools["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    assert "tool_choice" not in without_tools
    assert "tools" not in without_tools


def test_the_seed_is_never_sent(sdk: FakeSDK) -> None:
    sdk.script(_final())
    AnthropicModelAdapter(model="claude-haiku-4-5", seed=7, temperature=0.3).next_action(
        TRANSCRIPT, TOOLS
    )
    (request,) = sdk.requests
    assert "seed" not in request
    assert "seed" not in request.get("extra_body", {})
    assert "7" not in json.dumps(request.get("extra_body", {}))


def test_no_temperature_means_no_sampling_parameter_at_all(sdk: FakeSDK) -> None:
    sdk.script(_final())
    AnthropicModelAdapter().next_action(TRANSCRIPT, TOOLS)
    (request,) = sdk.requests
    assert "temperature" not in request
    assert "extra_body" not in request


def test_a_temperature_goes_in_extra_body_for_a_model_that_accepts_it(sdk: FakeSDK) -> None:
    """anthropic 1.x removed temperature from messages.create, so it is sent
    through extra_body, the SDK's pass-through for request fields."""
    sdk.script(_final())
    AnthropicModelAdapter(model="claude-haiku-4-5", temperature=0.2).next_action(TRANSCRIPT, TOOLS)
    (request,) = sdk.requests
    assert "temperature" not in request
    assert request["extra_body"] == {"temperature": 0.2}


@pytest.mark.parametrize(
    "model", ["claude-sonnet-5", "claude-opus-5", "claude-opus-5-5", "claude-fable-5-1"]
)
def test_a_temperature_for_a_model_that_rejects_it_fails_at_construction(
    sdk: FakeSDK, model: str
) -> None:
    """These models answer a non-default temperature with a 400 on every call.
    Refusing the configuration beats a run of model errors, and beats silently
    recording a temperature that was never applied."""
    assert model in FIXED_SAMPLING_MODELS
    with pytest.raises(AnthropicNotConfiguredError, match="temperature") as caught:
        AnthropicModelAdapter(model=model, temperature=0.0)
    assert model in str(caught.value)
    assert sdk.requests == []
    # Leaving it unset is fine.
    AnthropicModelAdapter(model=model)


def test_the_cli_refuses_a_temperature_sonnet_5_rejects(
    tmp_path,
    sdk: FakeSDK,
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv = ["--runs-dir", str(tmp_path / "runs"), "run-fixture", str(VALID_TASK_PATH)]
    assert main([*argv, "--provider", "anthropic", "--temperature", "0.2"]) == 2
    assert "claude-sonnet-5 rejects a non-default temperature" in capsys.readouterr().err
    assert sdk.requests == []


def test_run_fixture_marks_the_seed_it_could_not_send(tmp_path, sdk: FakeSDK) -> None:
    """The CLI path records the unsent seed the same way the suite path does."""
    sdk.script(_final())
    runs = tmp_path / "runs"
    argv = ["--runs-dir", str(runs), "run-fixture", str(VALID_TASK_PATH), "--seed", "7"]
    assert main([*argv, "--provider", "anthropic"]) == 0
    (config_path,) = runs.glob("*/run_config.json")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["seed"] == 7
    assert config["metadata"]["seed_sent"] is False
    assert "seed" not in sdk.requests[0]


def test_an_sdk_error_becomes_a_model_error(sdk: FakeSDK) -> None:
    sdk.script(FakeSDKError("overloaded"))
    with pytest.raises(ModelAdapterError, match="Anthropic API call failed: overloaded"):
        AnthropicModelAdapter().next_action(TRANSCRIPT, TOOLS)


# --- whole runs on a fake SDK: thinking round trip and billed errors ---


def _run(tmp_path, adapter: AnthropicModelAdapter):
    from trace_harness.environment.support_env import SupportEnvironment
    from trace_harness.runner.agent_runner import AgentRunner
    from trace_harness.runner.config import RunConfig
    from trace_harness.tasks.loader import load_docs_for_task, load_task
    from trace_harness.tracing.artifact_store import ArtifactStore

    task = load_task(VALID_TASK_PATH)
    environment = SupportEnvironment.from_task(task, docs=load_docs_for_task(task, VALID_TASK_PATH))
    store = ArtifactStore(tmp_path / "runs")
    config = RunConfig(task_id=task.task_id, provider="anthropic", model=adapter.model)
    return store, AgentRunner(adapter, environment, store).run(task, config)


def test_thinking_blocks_round_trip_through_a_run(tmp_path, sdk: FakeSDK) -> None:
    """The first turn thinks, then calls a tool. The second request has to carry
    that turn's thinking blocks unmodified in front of the tool_use, with the
    tool_result quoting its id (thinking docs, preserving thinking blocks)."""
    thinking = [
        FakeThinking(signature="sig-1"),
        FakeRedactedThinking(data="opaque"),
    ]
    sdk.script(
        FakeResponse(
            content=[
                *thinking,
                FakeText(text="Looking up the order."),
                FakeToolUse(name="get_order", input={"customer_name": "Casey"}, id="toolu_A"),
            ],
            stop_reason="tool_use",
        ),
        _final("Refund handled."),
    )
    _, result = _run(tmp_path, AnthropicModelAdapter(model="claude-sonnet-5"))
    assert result.status.value == "completed"
    first, second = sdk.requests
    assert [m["role"] for m in first["messages"]] == ["user"]
    assistant, tool_result = second["messages"][1], second["messages"][2]
    assert assistant == {
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "", "signature": "sig-1"},
            {"type": "redacted_thinking", "data": "opaque"},
            {
                "type": "tool_use",
                "id": "toolu_A",
                "name": "get_order",
                "input": {"customer_name": "Casey"},
            },
        ],
    }
    assert tool_result["content"][0]["tool_use_id"] == "toolu_A"


def test_a_billed_response_that_ends_the_run_is_still_in_the_trace_and_priced(
    tmp_path, sdk: FakeSDK
) -> None:
    """Two tool calls in one turn end the run as a model error, but the
    provider billed for the turn. The response has to reach the trace, and so
    the batch cost, or the run reports less than it spent."""
    from trace_harness.runner.batch import BatchRunner
    from trace_harness.runner.suite import AgentConfig, SuiteSpec
    from trace_harness.tracing.artifact_store import ArtifactStore

    usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
    sdk.script(
        FakeResponse(
            content=[
                FakeToolUse(name="get_order", input={}, id="toolu_A"),
                FakeToolUse(name="issue_refund", input={}, id="toolu_B"),
            ],
            stop_reason="tool_use",
            usage=usage,
        )
    )
    store = ArtifactStore(tmp_path / "runs")
    suite = SuiteSpec(
        suite_id="anthropic_billed_error",
        tasks=[str(VALID_TASK_PATH)],
        agent_configs=[AgentConfig(label="claude", provider="anthropic")],
    )
    (entry,) = BatchRunner(store).run(suite).entries
    assert entry.status == "error"
    assert entry.termination_reason == "model_error"
    kinds = [event.event_type.value for event in store.read_trace(entry.run_id)]
    assert kinds.index("model_response") < kinds.index("error")
    per_input, per_output = ANTHROPIC_PRICING["claude-sonnet-5"]
    assert entry.cost_usd == pytest.approx(per_input + per_output)
