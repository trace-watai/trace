"""Model adapters: the pluggable "brain" of a target agent.

The runner only ever talks to the :class:`~trace_harness.models.base.ModelAdapter`
protocol. Concrete adapters:

- :class:`~trace_harness.models.fixture.FixtureModelAdapter` — deterministic,
  scripted, no API keys. The default everywhere (tests, CI, fixtures).
- :class:`~trace_harness.models.gemini.GeminiModelAdapter` — scaffold for the
  team's early free-tier Gemini prototyping. Not implemented yet; fails with
  setup instructions instead of half-working API code.

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
) -> ModelAdapter:
    """Build a model adapter for ``provider``.

    ``fixture`` requires ``script_path`` (a FixtureScript JSON file).
    ``gemini`` requires ``GEMINI_API_KEY`` in the environment and is — by
    design — still a scaffold that raises on use.
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

        return GeminiModelAdapter(model=model)
    raise ValueError(f"unknown model provider '{provider}'; known providers: {KNOWN_PROVIDERS}")
