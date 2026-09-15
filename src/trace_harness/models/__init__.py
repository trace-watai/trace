"""Model adapters: the pluggable "brain" of a target agent.

The runner only ever talks to the :class:`~trace_harness.models.base.ModelAdapter`
protocol. Concrete adapters:

- :class:`~trace_harness.models.fixture.FixtureModelAdapter` — deterministic,
  scripted, no API keys. The default everywhere (tests, CI, fixtures).
- :class:`~trace_harness.models.gemini.GeminiModelAdapter` — native function
  calling through the optional ``google-genai`` SDK.

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

KNOWN_PROVIDERS = ("fixture", "gemini")


def resolve_model_name(provider: str, model: str | None, script_path: Path | str | None) -> str:
    """Resolve defaults once so adapter requests and persisted configs agree."""
    if provider == "fixture":
        if script_path is None:
            raise ValueError("provider 'fixture' needs a script")
        return f"scripted:{Path(script_path).stem}"
    if provider == "gemini":
        from trace_harness.models.gemini import DEFAULT_GEMINI_MODEL

        return model or DEFAULT_GEMINI_MODEL
    raise ValueError(f"unknown model provider '{provider}'; known providers: {KNOWN_PROVIDERS}")


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
) -> ModelAdapter:
    """Build a model adapter for ``provider``.

    ``fixture`` requires ``script_path`` (a FixtureScript JSON file).
    ``gemini`` requires ``GEMINI_API_KEY`` in the environment. Its behavioral
    knobs are passed explicitly so the adapter executes the same configuration
    persisted in ``run_config.json``.

    ``cassette`` explicitly selects record/replay. Replay constructs only the
    cassette adapter and requires neither the provider SDK nor its credentials.
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
        )
    raise ValueError(f"unknown model provider '{provider}'; known providers: {KNOWN_PROVIDERS}")
