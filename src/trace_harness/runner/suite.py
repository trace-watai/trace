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
    - ``max_cost_usd`` caps what the batch may spend on live calls (#196).
      ``BatchRunner`` checks it before each run through ``BudgetGuard``. A
      suite without it runs uncapped, as before.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

from trace_harness.models.cassette import CassetteConfig
from trace_harness.models.policy import CallPolicy

# 0.4.0: provider "external" with agent_ref
# 0.3.0: optional max_cost_usd and per-agent call_policy
# 0.2.0: optional cassette configuration per agent
SUITE_SCHEMA_VERSION = "0.4.0"


class AgentConfig(BaseModel):
    """One agent setup to run a suite under (provider + model + prompt knobs).

    ``label`` is the human-facing identifier used in batch summaries and must be
    unique within a suite so results can be attributed to the config that
    produced them.

    With ``provider: external`` the run drives an outside agent, and
    ``agent_ref`` names it as ``package.module:factory`` (see
    ``runner/target_agent.py``), and ``model`` optionally overrides the label
    the agent reports for itself. The outside agent owns its model, so such a
    config refuses ``cassette``, ``call_policy``, ``temperature`` and ``seed``,
    as the CLI refuses the matching flags with ``--agent``.
    """

    label: str
    provider: str = "fixture"
    model: str | None = None
    prompt_version: str | None = None
    temperature: float | None = None
    seed: int | None = None
    max_steps: int = Field(default=16, ge=1)
    timeout_seconds: float = Field(default=120.0, gt=0)
    cassette: CassetteConfig | None = None
    # Overrides the provider's default retry and rate-limit policy, for example
    # a higher requests_per_minute on a paid tier. Fields it leaves out keep the
    # provider's default. Ignored by runs that make no live call, and refused
    # for provider external, which makes none.
    call_policy: CallPolicy | None = None
    agent_ref: str | None = None

    @model_validator(mode="after")
    def _external_needs_agent_ref(self) -> AgentConfig:
        if self.provider == "external":
            if not self.agent_ref:
                raise ValueError("provider 'external' needs agent_ref (package.module:factory)")
            if self.cassette is not None:
                raise ValueError(
                    "provider 'external' cannot use a harness cassette; the outside agent "
                    "owns its model calls"
                )
            if self.call_policy is not None:
                raise ValueError(
                    "provider 'external' cannot use a call_policy; the outside agent owns its "
                    "model calls, so the harness has no request to retry or pace"
                )
            # The CLI refuses --temperature and --seed with --agent for the same
            # reason: run_config.json would record settings nothing applied.
            model_settings = [
                name for name in ("temperature", "seed") if getattr(self, name) is not None
            ]
            if model_settings:
                raise ValueError(
                    f"provider 'external' cannot set {' or '.join(model_settings)}; "
                    "the outside agent owns its model"
                )
        elif self.agent_ref is not None:
            raise ValueError("agent_ref is only valid with provider 'external'")
        return self


class SuiteSpec(BaseModel):
    """A named set of tasks and the agent configs to run them under."""

    schema_version: str = SUITE_SCHEMA_VERSION
    suite_id: str
    description: str | None = None
    tasks: list[str] = Field(min_length=1)
    agent_configs: list[AgentConfig] = Field(
        default_factory=lambda: [AgentConfig(label="fixture-baseline")], min_length=1
    )
    # Spend cap in USD across the batch's live runs. None runs uncapped.
    max_cost_usd: float | None = Field(default=None, ge=0)

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
