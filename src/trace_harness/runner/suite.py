"""Suite + agent-configuration schemas for batch execution.

A *suite* is an explicit list of runnable tasks crossed with one or more *agent
configurations*. Batch execution (``runner.batch``) runs the cartesian product
tasks × agent_configs, isolating per-run failures.

Design notes
    - ``tasks`` is an explicit list of fixture paths, never a glob: the task
      bank contains non-runnable counterexamples (under
      ``fixtures/tasks/counterexamples/``) that a glob would sweep in.
    - ``AgentConfig`` is the "agent configuration model" — the knobs that
      define one agent setup to sweep. It maps onto the per-run ``RunConfig``;
      the suite records the config so a batch is reproducible.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

SUITE_SCHEMA_VERSION = "0.1.0"


class AgentConfig(BaseModel):
    """One agent setup to run a suite under (provider + model + prompt knobs).

    ``label`` is the human-facing identifier used in batch summaries and must be
    unique within a suite so results can be attributed to the config that
    produced them.
    """

    label: str
    provider: str = "fixture"
    model: str | None = None
    prompt_version: str | None = None
    temperature: float | None = None
    seed: int | None = None
    max_steps: int = Field(default=16, ge=1)
    timeout_seconds: float = Field(default=120.0, gt=0)


class SuiteSpec(BaseModel):
    """A named set of tasks and the agent configs to run them under."""

    schema_version: str = SUITE_SCHEMA_VERSION
    suite_id: str
    description: str | None = None
    tasks: list[str] = Field(min_length=1)
    agent_configs: list[AgentConfig] = Field(
        default_factory=lambda: [AgentConfig(label="fixture-baseline")], min_length=1
    )

    @model_validator(mode="after")
    def _labels_unique(self) -> SuiteSpec:
        labels = [c.label for c in self.agent_configs]
        if len(labels) != len(set(labels)):
            dupes = sorted({label for label in labels if labels.count(label) > 1})
            raise ValueError(f"agent_configs have duplicate label(s): {dupes}")
        return self


class SuiteLoadError(ValueError):
    """A suite manifest was missing or malformed (reported as a CLI input error)."""


def load_suite(path: Path | str) -> SuiteSpec:
    """Load and validate a suite manifest JSON file.

    Task paths inside the manifest are kept verbatim (resolved at run time
    relative to the current working directory, like every other CLI task path).
    """
    p = Path(path)
    if not p.is_file():
        raise SuiteLoadError(f"suite manifest not found: {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SuiteLoadError(f"suite manifest is not valid JSON ({p}): {exc}") from exc
    try:
        return SuiteSpec.model_validate(data)
    except ValueError as exc:
        raise SuiteLoadError(f"invalid suite manifest ({p}): {exc}") from exc
