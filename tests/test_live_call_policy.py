"""The shared live call policy (#196): retries, backoff, rate limit, and the trace.

Offline like every other test. No provider SDK is imported and nothing sleeps:
each caller gets a fake clock whose ``sleep`` only advances time, a seeded or
disabled jitter, and its own rate limiter. Provider errors are small fakes that
carry what the real SDK errors carry (a status, a response with headers) under
the class names the policy reads.
"""

from __future__ import annotations

import json
import random
import socket
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from conftest import VALID_TASK_PATH
from trace_harness.models.anthropic import ANTHROPIC_PRICING, AnthropicModelAdapter
from trace_harness.models.anthropic import classify_error as anthropic_classify
from trace_harness.models.base import (
    ActionKind,
    Message,
    MessageRole,
    ModelAdapterError,
    ProviderNotConfiguredError,
    ToolSpec,
)
from trace_harness.models.cassette import CassetteConfig
from trace_harness.models.gemini import GeminiModelAdapter
from trace_harness.models.gemini import classify_error as gemini_classify
from trace_harness.models.openai import OpenAIModelAdapter
from trace_harness.models.openai import classify_error as openai_classify
from trace_harness.models.policy import (
    SHARED_RATE_LIMITER,
    CallPolicy,
    LiveCaller,
    ProviderCallError,
    RateLimiter,
    call_budget,
    default_call_policy,
    remaining_call_budget,
)
from trace_harness.runner.agent_runner import _call_with_timeout
from trace_harness.runner.batch import BatchRunner
from trace_harness.runner.pipeline import run_task_pipeline
from trace_harness.runner.suite import AgentConfig, SuiteSpec
from trace_harness.tracing.artifact_store import ArtifactStore

# --- fakes -----------------------------------------------------------------


class FakeClock:
    """Monotonic time that only moves when something sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class APIError(Exception):
    """Stands in for an SDK error base (anthropic.APIError, google-genai's APIError)."""


class APIStatusError(APIError):
    """Shaped like anthropic's and openai's status errors."""

    def __init__(
        self, status: int, *, headers: dict[str, str] | None = None, code: str | None = None
    ) -> None:
        super().__init__(f"Error code: {status}")
        self.status_code = status
        self.response = SimpleNamespace(headers=headers or {})
        self.code = code


class RateLimitError(APIStatusError):
    def __init__(self, **kw: Any) -> None:
        super().__init__(429, **kw)


class OverloadedError(APIStatusError):
    def __init__(self) -> None:
        super().__init__(529)


class APIConnectionError(APIError):
    pass


class APITimeoutError(APIConnectionError):
    pass


class OpenAIError(Exception):
    """openai's error base."""


class OpenAIStatusError(OpenAIError):
    def __init__(self, status: int, *, code: str | None = None) -> None:
        super().__init__(f"Error code: {status}")
        self.status_code = status
        self.code = code
        self.response = SimpleNamespace(headers={})


class GenaiAPIError(Exception):
    """Shaped like google.genai.errors.APIError: the status is ``code``."""

    def __init__(self, code: int, details: dict | None = None) -> None:
        super().__init__(f"{code} error")
        self.code = code
        self.details = details or {}
        self.response = None


GenaiAPIError.__name__ = "APIError"


# httpx's transport errors, which google-genai lets through unwrapped.
class TransportError(Exception):
    pass


class TimeoutException(TransportError):
    pass


class ReadTimeout(TimeoutException):
    pass


class NetworkError(TransportError):
    pass


class ConnectError(NetworkError):
    pass


class RemoteProtocolError(TransportError):
    pass


def caller(
    policy: CallPolicy | None = None,
    *,
    provider: str = "anthropic",
    clock: FakeClock | None = None,
    rng: random.Random | None = None,
    limiter: RateLimiter | None = None,
    budget: float | None = None,
) -> tuple[LiveCaller, FakeClock]:
    clock = clock or FakeClock()
    live = LiveCaller(
        provider,
        policy or CallPolicy(jitter=False),
        clock=clock,
        sleep=clock.sleep,
        rng=rng,
        limiter=limiter or RateLimiter(),
        budget_seconds=budget,
    )
    return live, clock


def scripted(*outcomes: Any):
    """A call that raises each exception in turn, then returns the next value."""
    calls: list[int] = []
    remaining = list(outcomes)

    def fn() -> Any:
        calls.append(1)
        item = remaining.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    return fn, calls


def classify(exc: Exception):
    return anthropic_classify(exc)


# --- the retry rules --------------------------------------------------------


def test_two_transient_failures_then_success_records_both_attempts() -> None:
    live, clock = caller()
    fn, calls = scripted(RateLimitError(), APIStatusError(503), "response")

    value, record = live.call(fn, classify)

    assert value == "response"
    assert len(calls) == 3
    assert record.outcome == "ok"
    assert record.attempts == 3
    assert [(f.attempt, f.error_class, f.status_code, f.transient) for f in record.failures] == [
        (1, "RateLimitError", 429, True),
        (2, "APIStatusError", 503, True),
    ]
    # No jitter: 2s then 4s, exactly what was slept.
    assert [f.delay_seconds for f in record.failures] == [2.0, 4.0]
    assert clock.sleeps == [2.0, 4.0]


@pytest.mark.parametrize(
    "error",
    [
        APIStatusError(408),
        APIStatusError(409),
        RateLimitError(),
        APIStatusError(500),
        APIStatusError(502),
        APIStatusError(503),
        APIStatusError(504),
        OverloadedError(),
        APIConnectionError("connection reset"),
        APITimeoutError("read timed out"),
        ConnectionResetError(54, "Connection reset by peer"),
        TimeoutError("timed out"),
    ],
    ids=lambda e: f"{type(e).__name__}-{getattr(e, 'status_code', '')}",
)
def test_transient_errors_are_retried(error: Exception) -> None:
    live, _ = caller()
    fn, calls = scripted(error, "response")
    value, record = live.call(fn, classify)
    assert value == "response"
    assert len(calls) == 2
    assert record.failures[0].transient is True


@pytest.mark.parametrize(
    "error",
    [ReadTimeout("read"), ConnectError("refused"), RemoteProtocolError("dropped")],
    ids=lambda e: type(e).__name__,
)
def test_httpx_transport_errors_gemini_lets_through_are_retried(error: Exception) -> None:
    live, _ = caller(provider="gemini")
    fn, calls = scripted(error, "response")
    _, record = live.call(fn, gemini_classify)
    assert len(calls) == 2
    assert record.failures[0].error_class == type(error).__name__


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 422, 501])
def test_permanent_statuses_are_never_retried(status: int) -> None:
    live, clock = caller()
    fn, calls = scripted(APIStatusError(status), "never reached")

    with pytest.raises(ProviderCallError) as caught:
        live.call(fn, classify)

    assert len(calls) == 1
    assert clock.sleeps == []
    record = caught.value.call_record
    assert record["outcome"] == "permanent_error"
    assert record["attempts"] == 1
    assert record["failures"][0]["transient"] is False
    assert record["failures"][0]["status_code"] == status
    assert record["failures"][0]["delay_seconds"] is None
    # Still the runner's clean model_error termination.
    assert isinstance(caught.value, ModelAdapterError)


def test_a_status_less_sdk_error_is_permanent() -> None:
    """An SDK error the adapter always mapped to a model error stays one, unretried."""
    live, _ = caller()
    fn, calls = scripted(APIError("response did not validate"), "never reached")
    with pytest.raises(ProviderCallError):
        live.call(fn, classify)
    assert len(calls) == 1


def test_provider_not_configured_is_never_retried_or_wrapped() -> None:
    live, clock = caller()
    fn, calls = scripted(ProviderNotConfiguredError("no key"), "never reached")
    with pytest.raises(ProviderNotConfiguredError) as caught:
        live.call(fn, classify)
    assert not isinstance(caught.value, ModelAdapterError)
    assert len(calls) == 1
    assert clock.sleeps == []


def test_a_harness_bug_is_never_retried_or_disguised_as_a_model_error() -> None:
    live, _ = caller()
    fn, calls = scripted(TypeError("bad request construction"), "never reached")
    with pytest.raises(TypeError):
        live.call(fn, classify)
    assert len(calls) == 1


def test_retries_stop_at_max_attempts() -> None:
    live, clock = caller(CallPolicy(max_attempts=3, jitter=False))
    fn, calls = scripted(RateLimitError(), RateLimitError(), RateLimitError(), "never reached")

    with pytest.raises(ProviderCallError, match="after 3 attempts") as caught:
        live.call(fn, classify)

    assert len(calls) == 3
    assert clock.sleeps == [2.0, 4.0]
    record = caught.value.call_record
    assert record["outcome"] == "retries_exhausted"
    assert record["attempts"] == 3
    assert [f["delay_seconds"] for f in record["failures"]] == [2.0, 4.0, None]


def test_backoff_is_capped_at_max_delay() -> None:
    policy = CallPolicy(
        max_attempts=4,
        initial_delay_seconds=2.0,
        backoff_multiplier=3.0,
        max_delay_seconds=5.0,
        jitter=False,
    )
    live, clock = caller(policy)
    fn, _ = scripted(RateLimitError(), RateLimitError(), RateLimitError(), "ok")
    live.call(fn, classify)
    assert clock.sleeps == [2.0, 5.0, 5.0]


def _jittered_delays(seed: int) -> list[float]:
    live, clock = caller(CallPolicy(max_attempts=5), rng=random.Random(seed))
    fn, _ = scripted(*[APIStatusError(503)] * 4, "ok")
    live.call(fn, classify)
    return clock.sleeps


def test_jitter_is_reproducible_from_a_seeded_generator() -> None:
    first, again, other = _jittered_delays(7), _jittered_delays(7), _jittered_delays(8)
    assert first == again
    assert first != other
    # Each delay lands in 50% to 100% of the unjittered 2, 4, 8, 16.
    for delay, base in zip(first, [2.0, 4.0, 8.0, 16.0], strict=True):
        assert base / 2 <= delay <= base


def test_a_retry_after_header_raises_the_delay_to_the_providers_hint() -> None:
    live, clock = caller()
    fn, _ = scripted(RateLimitError(headers={"retry-after": "7"}), "ok")
    _, record = live.call(fn, classify)
    assert clock.sleeps == [7.0]
    assert record.failures[0].retry_after_seconds == 7.0


def test_retry_after_ms_is_read_in_milliseconds() -> None:
    live, clock = caller()
    fn, _ = scripted(RateLimitError(headers={"retry-after-ms": "3500"}), "ok")
    live.call(fn, classify)
    assert clock.sleeps == [3.5]


def test_geminis_retry_delay_detail_is_its_hint() -> None:
    error = GenaiAPIError(
        429,
        details={
            "error": {
                "code": 429,
                "status": "RESOURCE_EXHAUSTED",
                "details": [
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "17s"}
                ],
            }
        },
    )
    verdict = gemini_classify(error)
    assert verdict is not None
    assert (verdict.transient, verdict.status_code, verdict.retry_after_seconds) == (
        True,
        429,
        17.0,
    )
    live, clock = caller(provider="gemini")
    fn, _ = scripted(error, "ok")
    live.call(fn, gemini_classify)
    assert clock.sleeps == [17.0]


def test_geminis_status_is_read_from_code() -> None:
    assert gemini_classify(GenaiAPIError(503)).transient is True
    assert gemini_classify(GenaiAPIError(400)).transient is False


def test_openai_insufficient_quota_is_permanent_and_a_plain_429_is_not() -> None:
    quota = OpenAIStatusError(429, code="insufficient_quota")
    assert openai_classify(quota).transient is False
    assert openai_classify(OpenAIStatusError(429, code="rate_limit_exceeded")).transient is True
    live, _ = caller(provider="openai")
    fn, calls = scripted(quota, "never reached")
    with pytest.raises(ProviderCallError):
        live.call(fn, openai_classify)
    assert len(calls) == 1


# --- the time budget --------------------------------------------------------


def test_a_backoff_that_would_pass_the_budget_is_not_slept() -> None:
    live, clock = caller(CallPolicy(initial_delay_seconds=8.0, jitter=False), budget=5.0)
    fn, calls = scripted(APIStatusError(503), "never reached")

    with pytest.raises(ProviderCallError, match="past the run's remaining time") as caught:
        live.call(fn, classify)

    assert len(calls) == 1
    assert clock.sleeps == []
    record = caught.value.call_record
    assert record["outcome"] == "deadline"
    assert record["failures"][0]["delay_seconds"] is None


def test_the_runners_budget_overrides_the_adapters_fallback() -> None:
    """Inside call_budget the run's remaining time applies, even when the
    adapter's own fallback budget is larger."""
    live, clock = caller(CallPolicy(initial_delay_seconds=8.0, jitter=False), budget=120.0)
    fn, calls = scripted(APIStatusError(503), "never reached")
    with call_budget(5.0), pytest.raises(ProviderCallError):
        live.call(fn, classify)
    assert len(calls) == 1
    assert clock.sleeps == []


def test_the_runner_hands_its_timeout_to_the_call_as_the_budget() -> None:
    """_call_with_timeout sets the budget inside the thread that makes the call."""
    seen = _call_with_timeout(remaining_call_budget, 3.0)
    assert seen is not None
    assert 2.5 < seen <= 3.0
    assert remaining_call_budget() is None


# --- the rate limit ---------------------------------------------------------


def test_calls_are_spaced_by_the_providers_rate_limit() -> None:
    clock = FakeClock()
    limiter = RateLimiter()
    policy = CallPolicy(requests_per_minute=30.0, jitter=False)  # one every 2s
    live, _ = caller(policy, clock=clock, limiter=limiter)

    _, first = live.call(lambda: "a", classify)
    _, second = live.call(lambda: "b", classify)

    assert first.rate_limit_wait_seconds == 0.0
    assert second.rate_limit_wait_seconds == 2.0
    assert clock.sleeps == [2.0]


def test_the_rate_limit_is_shared_by_every_caller_for_a_provider() -> None:
    """A batch builds a fresh adapter per cell, so pacing has to outlive the adapter."""
    clock = FakeClock()
    limiter = RateLimiter()
    policy = CallPolicy(requests_per_minute=30.0, jitter=False)
    first, _ = caller(policy, clock=clock, limiter=limiter)
    second, _ = caller(policy, clock=clock, limiter=limiter)
    other_provider, _ = caller(policy, provider="openai", clock=clock, limiter=limiter)

    first.call(lambda: "a", classify)
    _, record = second.call(lambda: "b", classify)
    assert record.rate_limit_wait_seconds == 2.0
    _, unrelated = other_provider.call(lambda: "c", classify)
    assert unrelated.rate_limit_wait_seconds == 0.0


def test_a_retry_takes_a_rate_limit_slot_too() -> None:
    policy = CallPolicy(requests_per_minute=60.0, initial_delay_seconds=0.0, jitter=False)
    live, clock = caller(policy)
    fn, _ = scripted(RateLimitError(), "ok")
    _, record = live.call(fn, classify)
    assert record.rate_limit_wait_seconds == 1.0
    assert clock.now == 1.0


def test_a_rate_limit_wait_past_the_budget_sends_nothing() -> None:
    clock = FakeClock()
    limiter = RateLimiter()
    policy = CallPolicy(requests_per_minute=6.0, jitter=False)  # one every 10s
    live, _ = caller(policy, clock=clock, limiter=limiter, budget=5.0)
    live.call(lambda: "first", classify)
    fn, calls = scripted("never sent")

    with pytest.raises(ProviderCallError, match="not attempted") as caught:
        live.call(fn, classify)

    assert calls == []
    assert caught.value.call_record["attempts"] == 0
    assert caught.value.call_record["outcome"] == "deadline"


def test_adapters_share_one_process_wide_limiter_by_default() -> None:
    """Without an injected caller, every adapter paces through the same limiter,
    so two cells of a batch cannot both take the same slot."""
    adapters = [
        AnthropicModelAdapter(api_key="k"),
        AnthropicModelAdapter(api_key="k"),
        OpenAIModelAdapter(api_key="k"),
        GeminiModelAdapter(api_key="k"),
    ]
    limiters = {id(adapter._caller._limiter) for adapter in adapters}
    assert limiters == {id(SHARED_RATE_LIMITER)}


def test_default_policies_pace_each_provider() -> None:
    for provider in ("gemini", "anthropic", "openai"):
        policy = default_call_policy(provider)
        assert policy.requests_per_minute is not None and policy.requests_per_minute > 0
        assert policy.max_attempts > 1


# --- the three adapters, with fake clients and no SDK ------------------------

TRANSCRIPT = [
    Message(role=MessageRole.SYSTEM, content="You are a support agent."),
    Message(role=MessageRole.USER, content="Refund me."),
]
TOOLS = [ToolSpec(name="get_order", description="Look up an order", parameters={})]


@dataclass
class ScriptedEndpoint:
    """One SDK method: raises each scripted error, then returns the response."""

    outcomes: list[Any]
    requests: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, **request: Any) -> Any:
        self.requests.append(request)
        item = self.outcomes.pop(0)
        if isinstance(item, threading.Event):
            # A request that hangs until the test releases it.
            item.wait(10)
            raise APIConnectionError("released after the run moved on")
        if isinstance(item, BaseException):
            raise item
        return item


@dataclass
class AnthropicText:
    text: str
    type: str = "text"


@dataclass
class AnthropicResponse:
    content: list[Any]
    stop_reason: str = "end_turn"
    usage: dict[str, int] = field(
        default_factory=lambda: {"input_tokens": 1000, "output_tokens": 100}
    )

    def model_dump(self, mode: str = "python") -> dict[str, Any]:
        return {
            "stop_reason": self.stop_reason,
            "content": [block.__dict__ for block in self.content],
            "usage": self.usage,
        }


@dataclass
class OpenAIMessage:
    content: str | None = None
    tool_calls: list[Any] | None = None


@dataclass
class OpenAIChoice:
    message: OpenAIMessage
    finish_reason: str = "stop"


@dataclass
class OpenAIResponse:
    choices: list[OpenAIChoice]
    usage: dict[str, int] = field(
        default_factory=lambda: {"prompt_tokens": 1000, "completion_tokens": 100}
    )

    def model_dump(self, mode: str = "python") -> dict[str, Any]:
        return {"choices": len(self.choices), "usage": self.usage}


class GeminiResponse:
    """A final-answer GenerateContentResponse with usage_metadata."""

    function_calls: list[Any] = []
    candidates: list[Any] = []

    def __init__(self, text: str, usage: dict[str, int] | None = None) -> None:
        self.text = text
        self.usage = usage or {"prompt_token_count": 1000, "candidates_token_count": 100}

    def model_dump(self, mode: str = "python") -> dict[str, Any]:
        return {"text": self.text, "usage_metadata": self.usage}


def anthropic_adapter(outcomes: list[Any], **kw: Any) -> tuple[AnthropicModelAdapter, Any]:
    endpoint = ScriptedEndpoint(outcomes)
    adapter = AnthropicModelAdapter(api_key="test-key", caller=caller(**kw)[0])
    adapter._client_obj = SimpleNamespace(messages=SimpleNamespace(create=endpoint))
    return adapter, endpoint


def openai_adapter(outcomes: list[Any], **kw: Any) -> tuple[OpenAIModelAdapter, Any]:
    endpoint = ScriptedEndpoint(outcomes)
    adapter = OpenAIModelAdapter(api_key="test-key", caller=caller(provider="openai", **kw)[0])
    adapter._client_obj = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=endpoint))
    )
    return adapter, endpoint


def gemini_adapter(outcomes: list[Any], **kw: Any) -> tuple[GeminiModelAdapter, Any]:
    endpoint = ScriptedEndpoint(outcomes)
    adapter = GeminiModelAdapter(
        model="gemini-2.5-flash",
        api_key="test-key",
        caller=caller(provider="gemini", **kw)[0],
    )
    adapter._client_obj = SimpleNamespace(models=SimpleNamespace(generate_content=endpoint))
    return adapter, endpoint


def test_the_anthropic_adapter_retries_through_the_policy() -> None:
    adapter, endpoint = anthropic_adapter(
        [RateLimitError(), OverloadedError(), AnthropicResponse([AnthropicText("Done.")])]
    )
    action = adapter.next_action(TRANSCRIPT, TOOLS)
    assert action.kind is ActionKind.FINAL_ANSWER
    assert len(endpoint.requests) == 3
    assert action.call_record is not None
    assert action.call_record["attempts"] == 3
    assert [f["status_code"] for f in action.call_record["failures"]] == [429, 529]
    assert action.raw is not None and action.raw["usage"]["input_tokens"] == 1000


def test_the_openai_adapter_retries_through_the_policy() -> None:
    adapter, endpoint = openai_adapter(
        [
            OpenAIStatusError(500),
            OpenAIStatusError(429),
            OpenAIResponse([OpenAIChoice(OpenAIMessage(content="Done."))]),
        ]
    )
    action = adapter.next_action(TRANSCRIPT, TOOLS)
    assert action.final_answer == "Done."
    assert len(endpoint.requests) == 3
    assert action.call_record["attempts"] == 3
    # The same request each time: a retry resends, it does not rebuild.
    assert endpoint.requests[0] == endpoint.requests[2]


def test_the_gemini_adapter_retries_through_the_policy_with_a_dict_config() -> None:
    adapter, endpoint = gemini_adapter(
        [GenaiAPIError(503), ConnectError("refused"), GeminiResponse("Done.")]
    )
    action = adapter.next_action(TRANSCRIPT, TOOLS)
    assert action.final_answer == "Done."
    assert action.call_record["attempts"] == 3
    assert [f["error_class"] for f in action.call_record["failures"]] == [
        "APIError",
        "ConnectError",
    ]
    request = endpoint.requests[0]
    assert request["model"] == "gemini-2.5-flash"
    assert request["config"]["automatic_function_calling"] == {"disable": True}
    assert request["config"]["system_instruction"] == "You are a support agent."
    assert request["config"]["tools"][0]["function_declarations"][0]["name"] == "get_order"


@pytest.mark.parametrize(
    ("build", "error", "prefix"),
    [
        (anthropic_adapter, APIStatusError(401), "Anthropic API call failed"),
        (openai_adapter, OpenAIStatusError(400), "OpenAI API call failed"),
        (gemini_adapter, GenaiAPIError(403), "Gemini API call failed"),
    ],
)
def test_a_permanent_provider_error_is_one_attempt_and_a_model_error(
    build, error: Exception, prefix: str
) -> None:
    adapter, endpoint = build([error, "never reached"])
    with pytest.raises(ModelAdapterError, match=prefix) as caught:
        adapter.next_action(TRANSCRIPT, TOOLS)
    assert len(endpoint.requests) == 1
    assert caught.value.call_record["outcome"] == "permanent_error"


def test_a_refusal_after_a_retry_is_not_retried_and_keeps_the_record() -> None:
    """The refusal is a normal response: it cost a request and is never retried."""
    refusal = AnthropicResponse([AnthropicText("I can't help with that.")], stop_reason="refusal")
    adapter, endpoint = anthropic_adapter([RateLimitError(), refusal, "never reached"])
    with pytest.raises(ModelAdapterError, match="refusal") as caught:
        adapter.next_action(TRANSCRIPT, TOOLS)
    assert len(endpoint.requests) == 2
    assert caught.value.call_record["attempts"] == 2
    assert caught.value.call_record["outcome"] == "ok"


def test_an_openai_content_filter_is_not_retried() -> None:
    filtered = OpenAIResponse(
        [OpenAIChoice(OpenAIMessage(content=None), finish_reason="content_filter")]
    )
    adapter, endpoint = openai_adapter([filtered, "never reached"])
    with pytest.raises(ModelAdapterError, match="content filter"):
        adapter.next_action(TRANSCRIPT, TOOLS)
    assert len(endpoint.requests) == 1


@pytest.mark.parametrize(
    ("build", "response", "usage_key"),
    [
        (
            anthropic_adapter,
            lambda: AnthropicResponse([AnthropicText("No.")], stop_reason="refusal"),
            "usage",
        ),
        (
            openai_adapter,
            lambda: OpenAIResponse(
                [OpenAIChoice(OpenAIMessage(content=None), finish_reason="content_filter")]
            ),
            "usage",
        ),
        (gemini_adapter, lambda: GeminiResponse(""), "usage_metadata"),
    ],
    ids=["anthropic_refusal", "openai_content_filter", "gemini_empty"],
)
def test_a_rejected_answer_keeps_the_billed_response_on_the_error(
    build, response, usage_key: str
) -> None:
    """The answer arrived and cost tokens before the adapter rejected it, so
    the error carries the raw response and its usage, beside the record."""
    adapter, endpoint = build([response()])
    with pytest.raises(ModelAdapterError) as caught:
        adapter.next_action(TRANSCRIPT, TOOLS)
    assert len(endpoint.requests) == 1
    assert caught.value.call_record["outcome"] == "ok"
    assert caught.value.raw is not None
    assert caught.value.raw[usage_key]


@pytest.mark.parametrize(
    ("module_name", "class_name", "build"),
    [
        ("anthropic", "Anthropic", lambda: AnthropicModelAdapter(api_key="k")),
        ("openai", "OpenAI", lambda: OpenAIModelAdapter(api_key="k")),
    ],
)
def test_sdk_clients_are_built_with_their_own_retries_off(
    monkeypatch: pytest.MonkeyPatch, module_name: str, class_name: str, build
) -> None:
    """The SDKs retry twice by default. Those attempts would be invisible to the
    record, so the policy is the only thing that retries."""
    built: dict[str, Any] = {}

    def client(**kwargs: Any) -> object:
        built.update(kwargs)
        return object()

    monkeypatch.setitem(sys.modules, module_name, SimpleNamespace(**{class_name: client}))
    build()._client()
    assert built["max_retries"] == 0


# --- the run and its trace ----------------------------------------------------


def _suite(provider: str, model: str, *, tasks: int = 1, **config: Any) -> SuiteSpec:
    return SuiteSpec(
        suite_id=f"{provider}_policy_probe",
        tasks=[str(VALID_TASK_PATH)] * tasks,
        agent_configs=[AgentConfig(label=provider, provider=provider, model=model, **config)],
    )


def _events(store: ArtifactStore, run_id: str, kind: str) -> list[dict[str, Any]]:
    lines = store.trace_path(run_id).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if json.loads(line)["event_type"] == kind]


def _live_gemini(outcomes: list[Any], built: list[Any]):
    """A create_model_adapter stand-in building the real Gemini adapter on a fake client."""

    def create(provider: str, **kwargs: Any) -> GeminiModelAdapter:
        assert provider == "gemini"
        clock = FakeClock()
        adapter = GeminiModelAdapter(
            model=kwargs["model"],
            api_key="test-key",
            caller=LiveCaller(
                provider,
                kwargs["call_policy"],
                clock=clock,
                sleep=clock.sleep,
                rng=random.Random(0),
                limiter=RateLimiter(),
            ),
        )
        endpoint = ScriptedEndpoint(outcomes)
        adapter._client_obj = SimpleNamespace(models=SimpleNamespace(generate_content=endpoint))
        built.append(endpoint)
        return adapter

    return create


def test_a_run_that_fails_twice_then_succeeds_completes_with_the_retries_in_its_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[Any] = []
    monkeypatch.setattr(
        "trace_harness.runner.pipeline.create_model_adapter",
        _live_gemini([RateLimitError(), GenaiAPIError(503), GeminiResponse("Done.")], built),
    )
    store = ArtifactStore(tmp_path / "runs")
    summary = BatchRunner(store).run(_suite("gemini", "gemini-2.5-flash"))

    entry = summary.entries[0]
    assert entry.status == "completed"
    assert len(built[0].requests) == 3
    responses = _events(store, entry.run_id, "model_response")
    assert len(responses) == 1
    record = responses[0]["payload"]["call_record"]
    assert record["attempts"] == 3
    assert record["outcome"] == "ok"
    assert [f["status_code"] for f in record["failures"]] == [429, 503]
    assert all(f["delay_seconds"] > 0 for f in record["failures"])
    # The normalized action stays as it was: the record sits beside raw.
    action = _events(store, entry.run_id, "model_action")[0]["payload"]
    assert "call_record" not in action and "raw" not in action
    # run_config.json says which policy the run executed.
    config = store.read_json(entry.run_id, "run_config.json")
    assert config["call_policy"] == default_call_policy("gemini").model_dump(mode="json")
    assert entry.cost_usd is not None and entry.cost_usd > 0


def test_a_run_whose_retries_run_out_ends_as_a_model_error_with_the_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[Any] = []
    monkeypatch.setattr(
        "trace_harness.runner.pipeline.create_model_adapter",
        _live_gemini([GenaiAPIError(429)] * 3, built),
    )
    store = ArtifactStore(tmp_path / "runs")
    suite = _suite("gemini", "gemini-2.5-flash", call_policy=CallPolicy(max_attempts=3))
    entry = BatchRunner(store).run(suite).entries[0]

    assert entry.status == "error"
    assert entry.termination_reason == "model_error"
    assert len(built[0].requests) == 3
    error = _events(store, entry.run_id, "error")[0]["payload"]
    assert error["kind"] == "model_error"
    assert error["call_record"]["outcome"] == "retries_exhausted"
    assert error["call_record"]["attempts"] == 3
    # A suite's override is what the run config records.
    config = store.read_json(entry.run_id, "run_config.json")
    assert config["call_policy"]["max_attempts"] == 3


@pytest.mark.parametrize("mode", [None, "record"])
def test_a_rejected_answer_is_in_the_trace_and_priced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str | None
) -> None:
    """A refusal ends the run as a model_error, and the billed answer is a
    model_response before it, so the run's cost counts it. Recording keeps
    only its token counts, as it does for an accepted answer."""
    refusal = AnthropicResponse([AnthropicText("No.")], stop_reason="refusal")
    monkeypatch.setattr(
        "trace_harness.models.anthropic.AnthropicModelAdapter",
        lambda **kw: anthropic_adapter([refusal])[0],
    )
    cassette = None
    if mode is not None:
        cassette = CassetteConfig(mode=mode, directory=str(tmp_path / "cassettes"))
    store = ArtifactStore(tmp_path / "runs")
    suite = _suite("anthropic", "claude-sonnet-5", cassette=cassette)
    entry = BatchRunner(store).run(suite).entries[0]

    assert entry.status == "error"
    assert entry.termination_reason == "model_error"
    [response] = _events(store, entry.run_id, "model_response")
    [error] = _events(store, entry.run_id, "error")
    assert response["step_id"] == error["step_id"] == 1
    assert response["payload"]["raw"]["usage"] == refusal.usage
    assert response["payload"]["call_record"]["outcome"] == "ok"
    assert error["payload"]["kind"] == "model_error"
    assert "call_record" not in error["payload"]
    per_input, per_output = ANTHROPIC_PRICING["claude-sonnet-5"]
    expected = (1000 * per_input + 100 * per_output) / 1_000_000
    assert entry.cost_usd == pytest.approx(expected)


def test_a_call_abandoned_at_the_timeout_keeps_its_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first attempt got a 503 and the second hung past the run's time.
    The model_timeout error lists both, and the run's cost stays unknown,
    because the hung request may still be billed."""
    release = threading.Event()
    built: list[Any] = []
    monkeypatch.setattr(
        "trace_harness.runner.pipeline.create_model_adapter",
        _live_gemini([GenaiAPIError(503), release], built),
    )
    policy = CallPolicy(initial_delay_seconds=0.01, jitter=False, requests_per_minute=6000.0)
    suite = _suite("gemini", "gemini-2.5-flash", call_policy=policy, timeout_seconds=0.5)
    store = ArtifactStore(tmp_path / "runs")
    try:
        entry = BatchRunner(store).run(suite).entries[0]
    finally:
        release.set()

    assert entry.termination_reason == "timeout"
    assert len(built[0].requests) == 2
    [error] = _events(store, entry.run_id, "error")
    assert error["payload"]["kind"] == "model_timeout"
    record = error["payload"]["call_record"]
    assert record["outcome"] == "abandoned"
    assert record["attempts"] == 2
    assert [(f["status_code"], f["delay_seconds"]) for f in record["failures"]] == [(503, 0.01)]
    assert _events(store, entry.run_id, "model_response") == []
    assert entry.cost_usd is None


def test_a_timeout_with_no_live_call_records_no_attempts() -> None:
    """A fixture-style adapter makes no request, so there is nothing to list."""
    release = threading.Event()
    try:
        with pytest.raises(Exception, match="did not return") as caught:
            _call_with_timeout(lambda: release.wait(10), 0.05)
    finally:
        release.set()
    assert caught.value.call_record is None


def test_a_fixture_run_records_no_policy_and_no_call_record(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "runs")
    result = run_task_pipeline(VALID_TASK_PATH, AgentConfig(label="fixture"), store)
    assert result.run_config.call_policy is None
    assert _events(store, result.run_result.run_id, "model_response") == []
    for event in _events(store, result.run_result.run_id, "model_action"):
        assert "call_record" not in event["payload"]


# --- cassettes -------------------------------------------------------------------


def _forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("replay attempted to construct a provider or open a socket")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr("trace_harness.models.gemini.GeminiModelAdapter", forbidden)


def test_a_cassette_replays_the_same_retry_record_without_calling_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[Any] = []
    live_create = _live_gemini([RateLimitError(), GeminiResponse("Done.")], built)
    monkeypatch.setattr(
        "trace_harness.models.gemini.GeminiModelAdapter",
        lambda **kw: live_create("gemini", **kw),
    )
    directory = str(tmp_path / "cassettes")

    def run(mode: str, runs: str):
        config = AgentConfig(
            label="gemini",
            provider="gemini",
            model="gemini-2.5-flash",
            cassette=CassetteConfig(mode=mode, directory=directory),
        )
        store = ArtifactStore(tmp_path / runs)
        return store, run_task_pipeline(VALID_TASK_PATH, config, store)

    recorded_store, recorded = run("record", "recorded")
    assert len(built) == 1 and len(built[0].requests) == 2

    _forbid_network(monkeypatch)
    replayed_store, replayed = run("replay", "replayed")

    assert replayed.run_result.status == "completed"
    before = _events(recorded_store, recorded.run_result.run_id, "model_response")
    after = _events(replayed_store, replayed.run_result.run_id, "model_response")
    assert [e["payload"] for e in before] == [e["payload"] for e in after]
    assert after[0]["payload"]["call_record"]["attempts"] == 2
    # Recording is live and runs under the policy; replay calls nothing.
    assert recorded.run_config.call_policy is not None
    assert replayed.run_config.call_policy is None
    assert len(built) == 1


@pytest.mark.parametrize(
    ("provider", "model", "response"),
    [
        ("gemini", "gemini-2.5-flash", lambda: GeminiResponse("Done.")),
        ("anthropic", "claude-sonnet-5", lambda: AnthropicResponse([AnthropicText("Done.")])),
        (
            "openai",
            "gpt-5",
            lambda: OpenAIResponse([OpenAIChoice(OpenAIMessage(content="Done."))]),
        ),
    ],
)
def test_a_live_run_is_priced_for_every_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str, model: str, response
) -> None:
    """A live run-suite entry has a numeric cost for each provider, directly and
    through a recording cassette, which keeps each provider's token counts under
    its own key. Replaying that cassette calls nothing and costs nothing."""
    classes = {
        "gemini": "trace_harness.models.gemini.GeminiModelAdapter",
        "anthropic": "trace_harness.models.anthropic.AnthropicModelAdapter",
        "openai": "trace_harness.models.openai.OpenAIModelAdapter",
    }
    builders = {"gemini": gemini_adapter, "anthropic": anthropic_adapter, "openai": openai_adapter}
    monkeypatch.setattr(classes[provider], lambda **kw: builders[provider]([response()])[0])
    directory = str(tmp_path / "cassettes")
    costs = {}
    for mode in (None, "record", "replay"):
        cassette = None if mode is None else CassetteConfig(mode=mode, directory=directory)
        suite = _suite(provider, model, cassette=cassette)
        entry = BatchRunner(ArtifactStore(tmp_path / str(mode))).run(suite).entries[0]
        assert entry.status == "completed"
        costs[mode] = entry.cost_usd
    assert isinstance(costs[None], float) and costs[None] > 0
    assert costs["record"] == costs[None]
    assert costs["replay"] == 0.0
