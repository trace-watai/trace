"""Harness-level configuration loaded from environment variables.

Why this exists
    Every CLI entry point needs the same three answers: which model provider
    to use by default, where run artifacts go, and how loud logging should
    be. Centralising that here keeps ``os.environ`` reads out of the rest of
    the codebase.

Scope
    This is *harness* configuration (machine-local), not *run* configuration.
    Per-run knobs (max steps, temperature, seed, ...) live in
    :class:`trace_harness.runner.config.RunConfig` and are persisted with the
    run artifacts so runs stay reproducible.

Assumptions
    - The fixture provider is the default and must work with no API keys.
    - ``GEMINI_API_KEY`` is only required when the provider is explicitly
      ``gemini`` (enforced by the Gemini adapter, not here).

# TODO(Samrath/tracing): when artifact storage grows a real backend, this is
# where its connection settings should be surfaced.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel

DEFAULT_PROVIDER = "fixture"
DEFAULT_RUNS_DIR = "./runs"
DEFAULT_LOG_LEVEL = "INFO"


def load_env_file(path: Path | str = ".env") -> dict[str, str]:
    """Load ``KEY=VALUE`` lines from a dotenv-style file into ``os.environ``.

    Deliberately tiny instead of a ``python-dotenv`` dependency, but it
    handles the dotenv syntax people actually write: ``export KEY=...``
    prefixes, a UTF-8 BOM, surrounding quotes, and trailing `` # comments``
    on unquoted values. No interpolation, no multi-line values. Existing
    environment variables are never overridden, so shell exports always win.

    Returns the variables that were actually applied.
    """
    env_path = Path(path)
    applied: dict[str, str] = {}
    if not env_path.is_file():
        return applied
    # utf-8-sig: a BOM would otherwise become part of the first key's name.
    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]  # quoted: keep verbatim, quotes removed
        else:
            value = value.split(" #", 1)[0].rstrip()  # unquoted: drop inline comment
        if key and key not in os.environ:
            os.environ[key] = value
            applied[key] = value
    return applied


class HarnessConfig(BaseModel):
    """Machine-local settings shared by all CLI commands."""

    # Honest status: read from TRACE_MODEL_PROVIDER but not consumed yet —
    # `run-fixture` is fixture-only by definition. This becomes meaningful
    # when a provider-agnostic `run` command lands alongside the first live
    # adapter. TODO(Rupert/runner): wire it up then.
    model_provider: str = DEFAULT_PROVIDER
    runs_dir: Path = Path(DEFAULT_RUNS_DIR)
    log_level: str = DEFAULT_LOG_LEVEL

    @classmethod
    def from_env(cls) -> HarnessConfig:
        """Build config from ``TRACE_*`` environment variables (see .env.example).

        Empty-string values fall back to defaults: ``TRACE_RUNS_DIR=`` would
        otherwise become ``Path("")`` (the current directory) and scatter
        run dirs outside the gitignored ``runs/``.
        """
        return cls(
            model_provider=os.environ.get("TRACE_MODEL_PROVIDER") or DEFAULT_PROVIDER,
            runs_dir=Path(os.environ.get("TRACE_RUNS_DIR") or DEFAULT_RUNS_DIR),
            log_level=os.environ.get("TRACE_LOG_LEVEL") or DEFAULT_LOG_LEVEL,
        )
