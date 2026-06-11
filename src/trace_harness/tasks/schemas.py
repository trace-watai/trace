"""Pydantic schemas for tasks — the structured test scenarios TRACE runs.

A *task* is the unit of evaluation: it describes the world the agent starts
in (``initial_state``), what it may use (``available_tools``,
``available_docs``), what good behavior looks like (``expected_behavior``,
``forbidden_actions``, ``required_evidence``), and which deterministic
verifiers judge the outcome (``verifier_ids``).

Design notes
    - ``extra="forbid"`` so typos in JSON fixtures fail loudly at load time
      instead of silently producing a different scenario.
    - ``initial_state`` stays a plain dict here on purpose: its shape is
      owned by the *environment* that interprets it (for the first vertical
      slice, :class:`trace_harness.environment.state.SupportState`). Typed
      validation happens when the environment parses it.
    - ``severity`` expresses how bad it is *if an agent fails this task*,
      and seeds the severity of downstream failure artifacts.

# TODO(Emily/tasks): promote ``metadata.user_message`` to a first-class field
# once we settle on how multi-turn tasks represent the inbound conversation.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

TASK_SCHEMA_VERSION = "0.1.0"


class Severity(StrEnum):
    """Shared severity scale used by tasks, verifier checks, and failure cards."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


_SEVERITY_ORDER: dict[Severity, int] = {
    Severity.LOW: 0,
    Severity.MEDIUM: 1,
    Severity.HIGH: 2,
    Severity.CRITICAL: 3,
}


def max_severity(severities: Iterable[Severity]) -> Severity | None:
    """Return the highest severity in ``severities`` (None for an empty input)."""
    ranked = sorted(severities, key=lambda s: _SEVERITY_ORDER[s])
    return ranked[-1] if ranked else None


class TaskSpec(BaseModel):
    """A structured test scenario for a target agent.

    JSON examples live in ``fixtures/tasks/`` at the repo root
    (``refund_policy_failure.json`` and ``refund_policy_valid_cash.json``).
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: str = TASK_SCHEMA_VERSION
    task_id: str
    title: str
    description: str
    goal: str
    workflow_type: str
    initial_state: dict[str, Any]
    available_tools: list[str]
    available_docs: list[str]
    # Path to a docs fixture file, resolved relative to the task file's
    # directory. ``available_docs`` selects which doc_ids from that fixture
    # are loaded into the environment. Inline docs can also be placed under
    # ``initial_state["docs"]`` for fully self-contained tasks.
    docs_fixture: str | None = None
    expected_behavior: list[str] = Field(default_factory=list)
    forbidden_actions: list[str] = Field(default_factory=list)
    required_evidence: list[str] = Field(default_factory=list)
    targeted_failure_modes: list[str] = Field(default_factory=list)
    verifier_ids: list[str] = Field(default_factory=list)
    severity: Severity = Severity.MEDIUM
    # Known metadata keys used by the harness today:
    #   fixture_script          — path to a FixtureScript (relative to the task file)
    #   user_message            — the inbound user/customer message for the transcript
    #   positive_sibling_tasks  — list of {test_name, task_fixture, description}
    #                             used by regression materialization to prevent overblocking
    metadata: dict[str, Any] = Field(default_factory=dict)
