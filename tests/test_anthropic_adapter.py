"""Offline tests for the Anthropic adapter's pure logic (#160).

These never touch the network or need an API key, same repo-wide rule the
Gemini tests follow. The conversion helpers return plain dicts and
``_normalize_response`` is duck-typed, so a small fake response stands in for
the SDK object and the ``anthropic`` package is never imported. Only the live
``messages.create`` call needs the SDK, and that path is verified by running a
task with ``--provider anthropic``, never here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from trace_harness.models import KNOWN_PROVIDERS, estimate_cost_usd, resolve_model_name
from trace_harness.models.anthropic import (
    ANTHROPIC_PRICING,
    DEFAULT_ANTHROPIC_MODEL,
    DEFAULT_MAX_TOKENS,
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


def test_construction_with_key_sets_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    adapter = AnthropicModelAdapter()
    assert adapter.name == "anthropic"
    assert adapter.model == DEFAULT_ANTHROPIC_MODEL == "claude-sonnet-5"
    assert adapter.max_tokens == DEFAULT_MAX_TOKENS
    assert adapter.timeout_seconds == 120.0


def test_a_seed_is_recorded_even_though_it_is_never_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Messages API has no seed. run_config.json still records what was asked."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    assert AnthropicModelAdapter(seed=7).seed == 7


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


def test_a_tool_error_is_sent_as_the_result_with_is_error_set() -> None:
    """A failed tool call is behavior the verifier judges. It must reach the model."""
    _, messages = _transcript_to_messages(
        [
            Message(
                role=MessageRole.TOOL,
                content="no such order",
                metadata={"tool_name": "get_order", "result": None, "error": "no such order"},
            )
        ]
    )
    result = messages[0]["content"][0]
    assert result["is_error"] is True
    assert result["content"] == "no such order"


def test_a_string_tool_result_is_not_json_wrapped() -> None:
    _, messages = _transcript_to_messages(
        [
            Message(
                role=MessageRole.TOOL,
                content="",
                metadata={"tool_name": "search_docs", "result": "policy text", "error": None},
            )
        ]
    )
    assert messages[0]["content"][0]["content"] == "policy text"


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
    with pytest.raises(ModelAdapterError, match="parallel tool calls"):
        _normalize_response(response)


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
    assert estimate_cost_usd("anthropic", "claude-sonnet-5", raws) == pytest.approx(3.0)
    # The default Gemini model has no price, so its runs keep reporting null.
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
                model="claude-sonnet-5",
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
    this provider never receives."""
    import json

    store, summary = stubbed_suite_run
    entry = summary.entries[0]
    config = json.loads(
        (store.runs_dir / entry.run_id / "run_config.json").read_text(encoding="utf-8")
    )
    assert config["provider"] == "anthropic"
    assert config["model"] == "claude-sonnet-5"
    assert config["temperature"] == 0.2
    assert config["seed"] == 41
    assert config["max_steps"] == 8
    assert config["timeout_seconds"] == 30.0


def test_the_provider_response_precedes_the_normalized_action(stubbed_suite_run) -> None:
    """A reader has to be able to see what the vendor said before what we made
    of it, or the normalization is unfalsifiable."""
    import json

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
    per_input, per_output = ANTHROPIC_PRICING["claude-sonnet-5"]
    entry = summary.entries[0]
    assert entry.cost_usd == pytest.approx(per_input + per_output)
    assert summary.aggregates.known_cost_usd == pytest.approx(per_input + per_output)
