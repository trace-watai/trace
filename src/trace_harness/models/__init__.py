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

KNOWN_PROVIDERS = ("fixture", "gemini")


def create_model_adapter(
    provider: str,
    *,
    script_path: Path | str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    seed: int | None = None,
    timeout_seconds: float = 120.0,
) -> ModelAdapter:
    """Build a model adapter for ``provider``.

    ``fixture`` requires ``script_path`` (a FixtureScript JSON file).
    ``gemini`` requires ``GEMINI_API_KEY`` in the environment. Its behavioral
    knobs are passed explicitly so the adapter executes the same configuration
    persisted in ``run_config.json``.
    """
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
