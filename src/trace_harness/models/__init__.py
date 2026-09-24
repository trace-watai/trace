"""Model adapters: the pluggable "brain" of a target agent.

The runner only ever talks to the :class:`~trace_harness.models.base.ModelAdapter`
protocol. Concrete adapters:

- :class:`~trace_harness.models.fixture.FixtureModelAdapter` — deterministic,
  scripted, no API keys. The default everywhere (tests, CI, fixtures).
- :class:`~trace_harness.models.gemini.GeminiModelAdapter` — native function
  calling through the optional ``google-genai`` SDK.
- :class:`~trace_harness.models.anthropic.AnthropicModelAdapter` — native tool
  use through the optional ``anthropic`` SDK.
- :class:`~trace_harness.models.openai.OpenAIModelAdapter` — native function
  calling through the optional ``openai`` SDK, and the only one of the three
  that both accepts a seed and publishes a per-token price.

Three live vendors exist so a live result never depends on one key, and so the
two-model conditions in #158, #159 and #217 have something to compare. All
three send their SDK call through the shared retry, backoff and rate-limit rules
in :mod:`trace_harness.models.policy` (#196).

``create_model_adapter`` is the one place provider strings become adapters,
so the CLI and future API server never branch on provider names themselves.
"""

from __future__ import annotations

from pathlib import Path

from trace_harness.models.base import ModelAdapter
from trace_harness.models.cassette import (
    CassetteConfig,
    CassetteRequestConfig,
    RecordingModelAdapter,
    cassette_path,
)
from trace_harness.models.policy import LIVE_PROVIDERS, CallPolicy, merge_call_policy

KNOWN_PROVIDERS = ("fixture", "gemini", "anthropic", "openai")


def resolve_model_name(provider: str, model: str | None, script_path: Path | str | None) -> str:
    """Resolve defaults once so adapter requests and persisted configs agree."""
    if provider == "fixture":
        if script_path is None:
            raise ValueError("provider 'fixture' needs a script")
        return f"scripted:{Path(script_path).stem}"
    if provider == "gemini":
        from trace_harness.models.gemini import DEFAULT_GEMINI_MODEL

        return model or DEFAULT_GEMINI_MODEL
    if provider == "anthropic":
        from trace_harness.models.anthropic import DEFAULT_ANTHROPIC_MODEL

        return model or DEFAULT_ANTHROPIC_MODEL
    if provider == "openai":
        from trace_harness.models.openai import DEFAULT_OPENAI_MODEL

        return model or DEFAULT_OPENAI_MODEL
    raise ValueError(f"unknown model provider '{provider}'; known providers: {KNOWN_PROVIDERS}")


def makes_live_calls(provider: str, cassette: CassetteConfig | None = None) -> bool:
    """Whether a run under this configuration calls a provider (and can cost money).

    The fixture provider never does, and a cassette replay never constructs a
    provider at all. Recording does call one.
    """
    if provider not in LIVE_PROVIDERS:
        return False
    return cassette is None or cassette.mode == "record"


def resolve_call_policy(
    provider: str,
    override: CallPolicy | None = None,
    cassette: CassetteConfig | None = None,
) -> CallPolicy | None:
    """The call policy a run executes under, resolved once like the model name.

    None for a run that makes no live call, so ``run_config.json`` never claims
    a policy that did not apply. Otherwise the provider's default with every
    field the suite's override sets in its place, so an override that only
    raises ``max_attempts`` keeps the provider's pacing.
    """
    if not makes_live_calls(provider, cassette):
        return None
    return merge_call_policy(provider, override)


def create_model_adapter(
    provider: str,
    *,
    script_path: Path | str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    seed: int | None = None,
    timeout_seconds: float = 120.0,
    cassette: CassetteConfig | None = None,
    task_id: str | None = None,
    prompt_version: str = "v0",
    call_policy: CallPolicy | None = None,
) -> ModelAdapter:
    """Build a model adapter for ``provider``.

    ``fixture`` requires ``script_path`` (a FixtureScript JSON file).
    Each live provider requires its own key in the environment,
    ``GEMINI_API_KEY``, ``ANTHROPIC_API_KEY`` or ``OPENAI_API_KEY``. Its behavioral
    knobs are passed explicitly so the adapter executes the same configuration
    persisted in ``run_config.json``.

    ``cassette`` explicitly selects record/replay. Replay constructs only the
    cassette adapter and requires neither the provider SDK nor its credentials.

    ``call_policy`` is the retry and rate-limit policy a live adapter runs
    under, overlaid on the provider's default; None gives the default. The
    fixture provider and replay ignore it, since they make no call.
    """
    if cassette is not None:
        if not task_id:
            raise ValueError("cassette mode requires task_id")
        config = CassetteRequestConfig(
            task_id=task_id,
            provider=provider,
            model=resolve_model_name(provider, model, script_path),
            temperature=temperature,
            seed=seed,
            timeout_seconds=timeout_seconds,
            prompt_version=prompt_version,
        )
        # Replay branches before provider construction: no credentials or SDK.
        inner = None
        if cassette.mode == "record":
            inner = create_model_adapter(
                provider,
                script_path=script_path,
                model=model,
                temperature=temperature,
                seed=seed,
                timeout_seconds=timeout_seconds,
                call_policy=call_policy,
            )
        return RecordingModelAdapter(
            mode=cassette.mode,
            path=cassette_path(cassette.directory, config),
            config=config,
            inner=inner,
        )
    if provider == "fixture":
        from trace_harness.models.fixture import FixtureModelAdapter

        if script_path is None:
            raise ValueError(
                "provider 'fixture' needs a script: pass --script or set "
                "metadata.fixture_script on the task"
            )
        return FixtureModelAdapter.from_file(script_path)
    if provider == "gemini":
        from trace_harness.models.gemini import GeminiModelAdapter

        return GeminiModelAdapter(
            model=model,
            temperature=temperature,
            seed=seed,
            timeout_seconds=timeout_seconds,
            call_policy=call_policy,
        )
    if provider == "anthropic":
        from trace_harness.models.anthropic import AnthropicModelAdapter

        return AnthropicModelAdapter(
            model=model,
            temperature=temperature,
            seed=seed,
            timeout_seconds=timeout_seconds,
            call_policy=call_policy,
        )
    if provider == "openai":
        from trace_harness.models.openai import OpenAIModelAdapter

        return OpenAIModelAdapter(
            model=model,
            temperature=temperature,
            seed=seed,
            timeout_seconds=timeout_seconds,
            call_policy=call_policy,
        )
    raise ValueError(f"unknown model provider '{provider}'; known providers: {KNOWN_PROVIDERS}")


def estimate_cost_usd(provider: str, model: str, raws: list[dict]) -> float | None:
    """Price a run's recorded provider responses, or None when it cannot be priced.

    Dispatches per provider because token accounting and prices are facts about
    each vendor. A provider or model with no price returns None,
    which is what ``BatchRunEntry.cost_usd`` carries for such a run and is
    honest about the gap.
    """
    if provider == "gemini":
        from trace_harness.models.gemini import estimate_cost_usd as gemini_cost

        return gemini_cost(model, raws)
    if provider == "anthropic":
        from trace_harness.models.anthropic import estimate_cost_usd as anthropic_cost

        return anthropic_cost(model, raws)
    if provider == "openai":
        from trace_harness.models.openai import estimate_cost_usd as openai_cost

        return openai_cost(model, raws)
    return None


def is_priced(provider: str, model: str | None) -> bool:
    """Whether a live run of ``model`` can be given a cost at all.

    The budget guard asks before starting a live run under a cap, because a run
    that cannot be priced could pass the cap without the guard seeing it.
    """
    if model is None:
        return False
    if provider == "gemini":
        from trace_harness.models.gemini import GEMINI_PRICING

        return model in GEMINI_PRICING
    if provider == "anthropic":
        from trace_harness.models.anthropic import ANTHROPIC_PRICING

        return model in ANTHROPIC_PRICING
    if provider == "openai":
        from trace_harness.models.openai import OPENAI_PRICING

        return model in OPENAI_PRICING
    return False
