"""RunConfig: every knob that affects how one run executes.

RunConfig is persisted to ``runs/{run_id}/run_config.json`` so a run can be
reproduced exactly. If a setting changes agent behavior and is not in here,
that is a reproducibility bug — add it.

``temperature`` and ``seed`` are recorded but meaningless for the fixture
provider; they exist so real adapters have a home for them from day one.

Known metadata keys used by the harness today:
    task_fixture_path    — repo path of the task fixture the CLI ran
                           (regression artifacts use it for replay commands)
    fixture_script_path  — repo path of the fixture script used
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

RUN_CONFIG_SCHEMA_VERSION = "0.1.0"

# Version of the system/user prompt template built by
# runner.agent_runner.build_initial_transcript. Bump when that template
# changes so recorded runs say which prompt produced them.
PROMPT_VERSION = "v0"


class ToolMode(StrEnum):
    """How tools are exposed to the model.

    ``native`` — the provider's function-calling API (preferred).
    ``json``   — tools rendered into the prompt; model answers in JSON.
                 Fallback for providers without function calling.
    The fixture provider ignores this entirely.
    """

    NATIVE = "native"
    JSON = "json"


class RunConfig(BaseModel):
    schema_version: str = RUN_CONFIG_SCHEMA_VERSION
    task_id: str
    provider: str = "fixture"
    model: str | None = None
    max_steps: int = Field(default=16, ge=1)
    timeout_seconds: float = Field(default=120.0, gt=0)
    temperature: float | None = None
    seed: int | None = None
    prompt_version: str = PROMPT_VERSION
    tool_mode: ToolMode = ToolMode.NATIVE
    metadata: dict[str, Any] = Field(default_factory=dict)
