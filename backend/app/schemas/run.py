"""Run-level models: agent config, task snapshot, state snapshot, run metadata.

TRA-22. These are the non-event artifacts of a run. Storage layout across files
(runs/{run_id}/...) is owned by TRA-8; this module only defines the contracts.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .common import TRACE_SCHEMA_VERSION, RunStatus, StateLabel, WorkflowType


class AgentConfig(BaseModel):
    """Provider-agnostic description of the agent under test.

    MODEL-AGNOSTIC (team mandate): ``provider`` is a free string. Never couple
    this schema to a vendor SDK — the runner maps the string to an adapter.
    """

    model_config = ConfigDict(extra="allow")

    provider: str = Field(
        ..., description="LLM provider id, e.g. 'gemini' | 'openai' | 'anthropic' | 'local'. No SDK coupling."
    )
    model: str = Field(..., description="Model identifier as the provider names it.")
    params: dict[str, Any] = Field(
        default_factory=dict, description="Sampling/config params (temperature, max_tokens, ...)."
    )
    agent_framework: str | None = Field(None, description="e.g. 'langgraph' | 'react' | 'simple'.")
    max_steps: int | None = Field(None, description="Step budget for the run, if enforced.")


class TaskSnapshot(BaseModel):
    """Frozen copy of the TaskSpec used for THIS run, so a run stays reproducible
    even if the task definition later changes.

    OWNERSHIP: the authoritative ``TaskSpec`` is TRA-6 (Evaluation Core / Emily).
    This is a forward-compatible snapshot (``extra='allow'``) mirroring the
    canonical fields in Notion → "Trace Schema & Data Formats".
    """

    model_config = ConfigDict(extra="allow")

    task_id: str
    task_version: str | None = None
    workflow_type: WorkflowType | str
    prompt: str
    tools_available: list[str] = Field(default_factory=list)
    documents_available: list[str] = Field(default_factory=list)
    expected_outcome: dict[str, Any] = Field(default_factory=dict)
    verifier_spec: dict[str, Any] = Field(default_factory=dict)
    forbidden_actions: list[str] = Field(default_factory=list)
    failure_modes_targeted: list[str] = Field(default_factory=list)
    difficulty: str | None = None


class StateSnapshot(BaseModel):
    """Environment state captured at a point in the run — the initial/final state
    contract. ``state`` is the full structured environment (mock CRM, order DB, ...).
    """

    model_config = ConfigDict(extra="forbid")

    label: StateLabel
    state: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime | None = None
    digest: str | None = Field(None, description="Optional stable hash of `state` for quick diffing.")


class RunMetadata(BaseModel):
    """Top-level run descriptor. One per run; carries the schema version."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    task_id: str
    schema_version: str = TRACE_SCHEMA_VERSION
    status: RunStatus = RunStatus.running
    created_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None
    step_count: int = 0
    termination_reason: str | None = Field(
        None, description="Why the run ended (e.g. 'completed', 'max_steps', 'tool_error', 'exception')."
    )
    target_agent: str | None = Field(None, description="Human label for the agent under test.")
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
