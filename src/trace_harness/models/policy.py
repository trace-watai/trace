"""The live call policy: bounded retries, backoff, and a per-provider rate limit (#196).

Why this exists
    A sweep is hundreds of live calls. Before this module one 429 ended the
    batch entry, because no adapter retried and nothing paced calls. Every live
    adapter (Gemini, Anthropic, OpenAI) now sends its one SDK call through a
    :class:`LiveCaller`, so the rules below are shared by all three and live in
    one place. There is no shared adapter base class beyond this.

What is retried
    Only errors a later attempt can plausibly fix. :func:`classify_provider_error`
    sorts an exception into one of three kinds.

    - Transient, retried: HTTP 408, 409, 429 and every 5xx except 501, which
      covers Anthropic's 529 overloaded, plus connection failures that carry no
      status (a reset, refused or dropped connection, a network timeout, or a
      proxy that could not be reached).
    - Permanent, never retried: every other status, which covers 400 invalid
      request, 401 and 403 credentials, 404 unknown model, 413, 422 and 501.
      Also a provider SDK error with no status that is not a connection
      failure, and two transport errors a resend cannot fix: a request that
      breaks HTTP before it is sent (httpx's ``LocalProtocolError``) and a URL
      whose scheme httpx cannot send (``UnsupportedProtocol``). Anthropic and
      OpenAI wrap those two as a connection error, so the exception's cause is
      read as well. Adapters can narrow this further; OpenAI's 429
      ``insufficient_quota`` is permanent because an exhausted quota does not
      come back within a run.
    - Not a provider error, re-raised untouched and never retried: anything
      else. That includes ``ProviderNotConfiguredError`` and bugs in the
      harness itself, so a ``TypeError`` is never retried and never recorded
      as a model error.

    Refusals, content filters, and empty or blocked responses arrive as
    ordinary responses and are rejected by each adapter's
    ``_normalize_response`` after the call returns. That is outside the retried
    function, so they are never retried either.

How long it waits
    Exponential backoff from ``initial_delay_seconds``, capped at
    ``max_delay_seconds``. With ``jitter`` on, each delay is scaled into 50% to
    100% of that value by a ``random.Random``. A live adapter seeds it with the
    run's seed when the run has one, so a seeded run that meets the same
    failures sleeps the same delays; a run without a seed draws from an
    unseeded generator. Tests inject their own. Either way the record keeps
    the delay actually slept. A provider's own hint (``Retry-After``, or
    Gemini's ``retryDelay``) raises the delay to at least that hint, because a
    retry sent sooner than the provider asked is refused again.

The time budget
    The runner already bounds each model call by the run's remaining time
    (``_call_with_timeout``). It now also hands the caller that deadline
    through :func:`call_budget`, and the caller never starts a backoff sleep or
    a rate-limit wait that would end past it. It gives up instead, with outcome
    ``deadline``, and the step ends as ``model_error`` with every attempt
    recorded. An attempt already in flight when the budget runs out is still
    ended by the runner as ``model_timeout``, as before. Retry time therefore
    never runs past the budget unrecorded: the trace says either ``deadline``
    or ``model_timeout``. The caller keeps a running copy of its record in a
    :class:`CallProgress` the runner hands down with the budget, so a
    ``model_timeout`` error event still lists the attempts made before it,
    with outcome ``abandoned``.

Rate limit
    A minimum spacing between request starts, per provider, shared by the
    whole process, because a batch builds a fresh adapter for every cell and a
    provider's limit applies to the account. Every attempt, retries included,
    takes a slot. Only requests per minute are modeled; a token-per-minute
    limit surfaces as a 429 and goes through the retry path. A suite's
    ``call_policy`` overrides the provider default field by field, so one that
    only raises ``max_attempts`` keeps the provider's pacing.

What is recorded
    :class:`CallRecord` gives the requests sent, each failed attempt's error
    class, status, provider hint and the delay that followed it, the time spent
    waiting on the rate limit, and the outcome. The adapter puts it on
    ``AgentAction.call_record``, or on the ``ModelAdapterError`` when the call
    failed. The runner writes it into the ``model_response`` or ``error``
    event, and a cassette stores it, so a replay shows the same record without
    calling anything. Exception messages are left out of the record, since
    provider error strings can echo request headers.

    A response that arrives and is then rejected by the adapter (a refusal, a
    content filter, an empty answer, parallel tool calls) was still billed.
    The adapter's response normalizer puts the raw response on the error,
    :func:`with_call_record` puts the record beside it, and the runner writes
    both as a ``model_response`` event before the ``error``, so its usage is
    priced like any other response.

SDK retries are off
    The Anthropic and OpenAI clients retry twice by default, which would hide
    attempts from this record, so both are built with ``max_retries=0``.
    google-genai retries only when ``retry_options`` is set, and it is not.
"""

from __future__ import annotations

import contextlib
import contextvars
import math
import random
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from trace_harness.models.base import AgentAction, ModelAdapterError

T = TypeVar("T")

#: Requests per minute each live provider is paced to when a suite does not
#: override it. The lowest published tier for each vendor, kept as data so a
#: paid tier is a one-line ``call_policy`` override in the suite.
DEFAULT_REQUESTS_PER_MINUTE: dict[str, float] = {
    "gemini": 10.0,
    "anthropic": 50.0,
    "openai": 500.0,
}

#: Providers whose calls go through this policy. The fixture provider never does.
LIVE_PROVIDERS = frozenset(DEFAULT_REQUESTS_PER_MINUTE)

#: How each provider is named in error messages, which predate this policy.
_LABELS = {"gemini": "Gemini", "anthropic": "Anthropic", "openai": "OpenAI"}

#: Class names (anywhere in an exception's MRO) of transport failures that
#: carry no HTTP status. Anthropic and OpenAI wrap every such failure in the
#: first two. google-genai lets httpx's own exceptions through, which are the
#: last three. Matched by name so the classification needs no SDK installed.
CONNECTION_ERROR_NAMES = frozenset(
    {
        "APIConnectionError",
        "APITimeoutError",
        "TimeoutException",
        "NetworkError",
        "RemoteProtocolError",
        "ProxyError",
    }
)

#: httpx transport errors no resend can fix: a request that breaks HTTP before
#: it leaves the client, and a URL with a scheme httpx cannot send. Matched on
#: the exception and on its cause, since Anthropic and OpenAI raise their
#: connection error ``from`` the httpx one.
PERMANENT_TRANSPORT_ERROR_NAMES = frozenset({"LocalProtocolError", "UnsupportedProtocol"})

# "abandoned": the runner's timeout ended the call before the policy did,
# normally with the last attempt still in flight. Only a model_timeout error
# event carries it.
Outcome = Literal["ok", "permanent_error", "retries_exhausted", "deadline", "abandoned"]


class CallPolicy(BaseModel):
    """The retry, backoff and rate-limit rules one live run executes under.

    Persisted in ``run_config.json`` as ``call_policy``, so a run says how it
    was paced. A suite agent config may set it to override the defaults.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_attempts: int = Field(default=5, ge=1, le=20)
    initial_delay_seconds: float = Field(default=2.0, ge=0)
    max_delay_seconds: float = Field(default=30.0, ge=0)
    backoff_multiplier: float = Field(default=2.0, ge=1)
    jitter: bool = True
    # None means no pacing. Filled per provider by default_call_policy.
    requests_per_minute: float | None = Field(default=None, gt=0)


def default_call_policy(provider: str) -> CallPolicy:
    """The policy a live provider runs under when nothing overrides it."""
    return CallPolicy(requests_per_minute=DEFAULT_REQUESTS_PER_MINUTE.get(provider))


def merge_call_policy(provider: str, override: CallPolicy | None = None) -> CallPolicy:
    """The provider's default policy with every field ``override`` sets in its place.

    A field counts as set when the suite file or the constructor names it, even
    as null, so ``"requests_per_minute": null`` still turns pacing off on
    purpose, while an override that leaves it out keeps the provider's rate.
    """
    default = default_call_policy(provider)
    if override is None:
        return default
    return CallPolicy.model_validate(
        {**default.model_dump(), **override.model_dump(exclude_unset=True)}
    )


class FailedAttempt(BaseModel):
    """One request that raised, and what the caller did next."""

    model_config = ConfigDict(extra="forbid")

    attempt: int = Field(ge=1)
    error_class: str
    status_code: int | None = None
    transient: bool
    # What the provider asked for (Retry-After or retryDelay), if anything.
    retry_after_seconds: float | None = None
    # The backoff slept after this failure. None when the caller gave up instead.
    delay_seconds: float | None = None


class CallRecord(BaseModel):
    """How one model call went: what a trace shows about retries and pacing."""

    model_config = ConfigDict(extra="forbid")

    attempts: int = Field(ge=0)
    outcome: Outcome
    rate_limit_wait_seconds: float = 0.0
    failures: list[FailedAttempt] = Field(default_factory=list)


class ProviderCallError(ModelAdapterError):
    """A live call that failed under the policy. The runner records it as
    ``model_error`` and writes ``call_record`` into the error event."""

    def __init__(self, message: str, *, call_record: dict[str, Any]) -> None:
        super().__init__(message)
        self.call_record = call_record


@dataclass(frozen=True)
class ErrorVerdict:
    """What the policy makes of one provider exception."""

    transient: bool
    status_code: int | None = None
    retry_after_seconds: float | None = None

    def permanent(self) -> ErrorVerdict:
        return replace(self, transient=False)


def is_transient_status(status: int) -> bool:
    """408, 409, 429 and every 5xx except 501 Not Implemented."""
    return status in (408, 409, 429) or (500 <= status <= 599 and status != 501)


def _status_code(exc: BaseException) -> int | None:
    """The HTTP status an SDK error carries, whatever the SDK calls it.

    Anthropic and OpenAI use ``status_code``; google-genai uses ``code``.
    OpenAI's ``code`` is a string such as ``insufficient_quota``, which is why
    only an int in the HTTP range counts.
    """
    for attr in ("status_code", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599:
            return value
    return None


def retry_after_seconds(exc: BaseException) -> float | None:
    """Seconds from a ``Retry-After`` (or ``retry-after-ms``) response header.

    An HTTP-date value is not parsed and reads as no hint.
    """
    headers = getattr(getattr(exc, "response", None), "headers", None)
    get = getattr(headers, "get", None)
    if not callable(get):
        return None
    for name, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
        value = get(name)
        if value is None:
            continue
        try:
            seconds = float(value) * scale
        except (TypeError, ValueError):
            continue
        if math.isfinite(seconds) and seconds >= 0:
            return seconds
    return None


def _mro_names(exc: BaseException) -> set[str]:
    return {cls.__name__ for cls in type(exc).__mro__}


def classify_provider_error(
    exc: BaseException, *, sdk_error_names: frozenset[str]
) -> ErrorVerdict | None:
    """Sort an exception raised by an SDK call, or None when it is not a provider error.

    ``sdk_error_names`` are the class names of the provider SDK's error base,
    such as ``APIError``, so a status-less SDK error is still recognized as the
    provider's and treated as permanent.
    """
    status = _status_code(exc)
    names = _mro_names(exc)
    if status is not None:
        return ErrorVerdict(
            transient=is_transient_status(status),
            status_code=status,
            retry_after_seconds=retry_after_seconds(exc),
        )
    cause = exc.__cause__
    if (names | (_mro_names(cause) if cause is not None else set())) & (
        PERMANENT_TRANSPORT_ERROR_NAMES
    ):
        return ErrorVerdict(transient=False)
    if names & CONNECTION_ERROR_NAMES or isinstance(exc, ConnectionError | TimeoutError):
        return ErrorVerdict(transient=True)
    if names & sdk_error_names:
        return ErrorVerdict(transient=False)
    return None


# --- the time budget the runner hands down --------------------------------

_DEADLINE: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "trace_call_deadline", default=None
)


class CallProgress:
    """The record of a live call still under way, readable from another thread.

    The runner hands one down with the call budget and reads it when it
    abandons the call at its timeout. :class:`LiveCaller` replaces ``record``
    with a new dict at every step, so a reader always sees one whole record:
    the attempts sent so far, including one still in flight, and outcome
    ``abandoned``. None until the first request is about to go out.
    """

    def __init__(self) -> None:
        self.record: dict[str, Any] | None = None


_PROGRESS: contextvars.ContextVar[CallProgress | None] = contextvars.ContextVar(
    "trace_call_progress", default=None
)


@contextlib.contextmanager
def call_budget(seconds: float, progress: CallProgress | None = None) -> Iterator[None]:
    """Give model calls made inside this block ``seconds`` from now, in total.

    Set by the runner inside the thread that makes the call, so it is the same
    budget ``_call_with_timeout`` enforces from outside. ``progress``, when
    given, is where the call keeps its running record for the runner.
    """
    token = _DEADLINE.set(time.monotonic() + seconds)
    progress_token = _PROGRESS.set(progress)
    try:
        yield
    finally:
        _PROGRESS.reset(progress_token)
        _DEADLINE.reset(token)


def remaining_call_budget() -> float | None:
    """Seconds left in the enclosing :func:`call_budget`, or None outside one."""
    deadline = _DEADLINE.get()
    return None if deadline is None else deadline - time.monotonic()


# --- the per-provider rate limit -------------------------------------------


class RateLimiter:
    """Minimum spacing between request starts, per provider, safe across threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_start: dict[str, float] = {}

    def reserve(
        self,
        provider: str,
        requests_per_minute: float | None,
        *,
        now: float,
        deadline: float | None,
    ) -> float | None:
        """Claim the next request slot and return the seconds to wait for it.

        Returns None, claiming nothing, when that slot would start at or past
        ``deadline``.
        """
        with self._lock:
            start = now
            last = self._last_start.get(provider)
            if requests_per_minute is not None and last is not None:
                start = max(now, last + 60.0 / requests_per_minute)
            if deadline is not None and start >= deadline:
                return None
            self._last_start[provider] = start
            return start - now


#: The limiter every live adapter shares unless a test injects its own.
SHARED_RATE_LIMITER = RateLimiter()


# --- the caller -------------------------------------------------------------


class LiveCaller:
    """Makes one provider's SDK calls under a :class:`CallPolicy`.

    ``clock``, ``sleep``, ``rng`` and ``limiter`` are injectable so tests are
    deterministic and never sleep. ``budget_seconds`` is the fallback budget
    when no :func:`call_budget` encloses the call, which is the case only when
    an adapter is used outside the runner. Adapters build theirs with
    :func:`build_live_caller`.
    """

    def __init__(
        self,
        provider: str,
        policy: CallPolicy,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
        limiter: RateLimiter | None = None,
        budget_seconds: float | None = None,
    ) -> None:
        self.provider = provider
        self.policy = policy
        self.label = _LABELS.get(provider, provider)
        self._clock = clock
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._limiter = limiter or SHARED_RATE_LIMITER
        self._budget_seconds = budget_seconds

    def call(
        self,
        fn: Callable[[], T],
        classify: Callable[[Exception], ErrorVerdict | None],
    ) -> tuple[T, CallRecord]:
        """Run ``fn`` until it succeeds or the policy gives up.

        Returns the value and the record. Raises :class:`ProviderCallError`
        carrying the record when it gives up, or re-raises an exception
        ``classify`` does not recognize without retrying it.
        """
        budget = remaining_call_budget()
        if budget is None:
            budget = self._budget_seconds
        deadline = None if budget is None else self._clock() + budget
        progress = _PROGRESS.get()
        failures: list[FailedAttempt] = []
        waited = 0.0
        attempt = 0
        last_error: Exception | None = None

        def publish() -> None:
            # What the trace should say if the runner abandons the call now.
            if progress is not None:
                progress.record = CallRecord(
                    attempts=attempt,
                    outcome="abandoned",
                    rate_limit_wait_seconds=round(waited, 3),
                    failures=list(failures),
                ).model_dump(mode="json")

        while True:
            wait = self._limiter.reserve(
                self.provider,
                self.policy.requests_per_minute,
                now=self._clock(),
                deadline=deadline,
            )
            if wait is None:
                raise self._give_up("deadline", attempt, failures, waited, last_error)
            if wait > 0:
                self._sleep(wait)
                waited += wait
            attempt += 1
            publish()
            try:
                value = fn()
            except Exception as exc:
                verdict = classify(exc)
                if verdict is None:
                    raise
                last_error = exc
                failure = FailedAttempt(
                    attempt=attempt,
                    error_class=type(exc).__name__,
                    status_code=verdict.status_code,
                    transient=verdict.transient,
                    retry_after_seconds=verdict.retry_after_seconds,
                )
                failures.append(failure)
                if not verdict.transient:
                    raise self._give_up("permanent_error", attempt, failures, waited, exc) from exc
                if attempt >= self.policy.max_attempts:
                    raise self._give_up(
                        "retries_exhausted", attempt, failures, waited, exc
                    ) from exc
                delay = self._delay(attempt, verdict.retry_after_seconds)
                if deadline is not None and self._clock() + delay >= deadline:
                    raise self._give_up("deadline", attempt, failures, waited, exc) from exc
                failures[-1] = failure.model_copy(update={"delay_seconds": delay})
                publish()
                self._sleep(delay)
                continue
            record = CallRecord(
                attempts=attempt,
                outcome="ok",
                rate_limit_wait_seconds=round(waited, 3),
                failures=failures,
            )
            return value, record

    def _delay(self, attempt: int, hint: float | None) -> float:
        """Backoff after the ``attempt``-th failure, at least the provider's hint."""
        policy = self.policy
        delay = min(
            policy.max_delay_seconds,
            policy.initial_delay_seconds * policy.backoff_multiplier ** (attempt - 1),
        )
        if policy.jitter:
            delay *= 0.5 + 0.5 * self._rng.random()
        if hint is not None:
            delay = max(delay, hint)
        # Rounded before sleeping, so the record is exactly what was slept.
        return round(delay, 3)

    def _give_up(
        self,
        outcome: Outcome,
        attempts: int,
        failures: list[FailedAttempt],
        waited: float,
        cause: Exception | None,
    ) -> ProviderCallError:
        record = CallRecord(
            attempts=attempts,
            outcome=outcome,
            rate_limit_wait_seconds=round(waited, 3),
            failures=failures,
        )
        if outcome == "permanent_error":
            message = f"{self.label} API call failed: {cause}"
        elif outcome == "retries_exhausted":
            message = f"{self.label} API call failed after {attempts} attempts: {cause}"
        elif cause is None:
            message = (
                f"{self.label} API call not attempted: the rate-limit wait would pass "
                "the run's remaining time"
            )
        else:
            message = (
                f"{self.label} API call gave up after {attempts} attempt(s), because the "
                f"next one would start past the run's remaining time: {cause}"
            )
        return ProviderCallError(message, call_record=record.model_dump(mode="json"))


def build_live_caller(
    provider: str,
    call_policy: CallPolicy | None,
    *,
    seed: int | None,
    timeout_seconds: float,
) -> LiveCaller:
    """The caller a live adapter uses when a test does not inject one.

    It runs the provider default overlaid with ``call_policy`` (see
    :func:`merge_call_policy`). Its jitter generator is seeded with the run's
    seed when there is one, so the delays of a seeded run depend only on which
    attempts failed; with no seed it is unseeded.
    """
    return LiveCaller(
        provider,
        merge_call_policy(provider, call_policy),
        rng=None if seed is None else random.Random(seed),
        budget_seconds=timeout_seconds,
    )


def with_call_record(record: CallRecord, normalize: Callable[[], AgentAction]) -> AgentAction:
    """Normalize a response and attach how it was obtained.

    A response that normalizes into an error (a refusal, a blocked, empty or
    truncated answer, parallel tool calls) still cost a request, so the record
    rides on that error too. The normalizer has already put the raw response,
    with the usage it was billed for, on the error, and the runner writes both
    as a ``model_response`` event before the ``error``.
    """
    recorded = record.model_dump(mode="json")
    try:
        action = normalize()
    except ModelAdapterError as exc:
        exc.call_record = recorded
        raise
    return action.model_copy(update={"call_record": recorded})
