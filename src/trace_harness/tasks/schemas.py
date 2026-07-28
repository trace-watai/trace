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

Two layers of validation (keep them separate)
    - **Structural** (here): rules every ``TaskSpec`` must satisfy, including
      minimal unit-test stubs — slug/version/workflow shapes, no duplicate
      list entries, resolvable docs. Deliberately lenient about *content*
      (empty tool/verifier lists are allowed) so other modules can build stub
      tasks in their unit tests.
    - **Authoring quality** (``tasks/validation.py`` + the task-validity
      rubric, applied to real fixtures under ``fixtures/tasks/``): the
      stricter "is this a *good* task to evaluate on" checks — non-empty
      tools/verifiers/failure-modes, describes correct behavior, no clocks in
      state, multi-step. Those do NOT live here so they cannot break stubs.

# TODO(Emily/tasks): promote ``metadata.user_message`` to a first-class field
# once we settle on how multi-turn tasks represent the inbound conversation
# (touches the runner's transcript builder — coordinate with Rupert).
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

TASK_SCHEMA_VERSION = "0.3.0"


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


class Difficulty(StrEnum):
    """How hard a task is, for suite balance and reporting (Task Design Guide).

    Distinct from ``Severity``: difficulty is about how demanding the scenario
    is for the agent; severity is about how bad a *failure* would be. Optional on
    a task — an unset difficulty means "not yet calibrated", not "invalid".
    """

    EASY = "easy"  # single obvious tool path, no policy edge cases
    MEDIUM = "medium"  # some branching, lookups, or source selection
    HARD = "hard"  # policy edge cases, multi-step reasoning, traps


class TaskSpec(BaseModel):
    """A structured test scenario for a target agent.

    JSON examples live in ``fixtures/tasks/`` at the repo root
    (``refund_policy_failure.json`` and ``refund_policy_valid_cash.json``).
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(
        default=TASK_SCHEMA_VERSION,
        pattern=r"^\d+\.\d+\.\d+$",
        description="Semantic version of the task schema this fixture targets (e.g. '0.1.0').",
    )
    task_id: str = Field(
        ...,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
        description=(
            "Stable lowercase slug, unique across the task bank; matches the fixture file name."
        ),
    )
    title: str = Field(
        ...,
        min_length=1,
        description="Short one-line human label for dashboards and reports.",
    )
    description: str = Field(
        ...,
        min_length=1,
        description="Scenario context: the situation, the knowledge base, and why it is tricky.",
    )
    goal: str = Field(
        ...,
        min_length=1,
        description="The agent's objective in one sentence — what a correct run accomplishes.",
    )
    workflow_type: str = Field(
        ...,
        pattern=r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$",
        description=(
            "Dotted workflow family, lowercase (e.g. 'support.refund'). Free string for now; "
            "may become an enum once families settle — coordinate with runner/environment."
        ),
    )
    initial_state: dict[str, Any] = Field(
        ...,
        description=(
            "Complete starting world for the environment to load. Must be deterministic: no "
            "clocks/timestamps/randomness — encode time as relative ages (e.g. purchase_age_days)."
        ),
    )
    available_tools: list[str] = Field(
        ...,
        description="Tool names the agent may call; must match the environment's registered tools.",
    )
    available_docs: list[str] = Field(
        default_factory=list,
        description="Doc ids (from docs_fixture) to load; empty for pure tool tasks.",
    )
    docs_fixture: str | None = Field(
        default=None,
        description=(
            "Path to a docs fixture, relative to the task file. Required when available_docs "
            "is set and docs are not inline under initial_state['docs']."
        ),
    )
    expected_behavior: list[str] = Field(
        default_factory=list,
        description="Concrete, checkable statements describing what a correct run does.",
    )
    forbidden_actions: list[str] = Field(
        default_factory=list,
        description=(
            "Actions a correct run must avoid; violations are failures regardless of outcome."
        ),
    )
    required_evidence: list[str] = Field(
        default_factory=list,
        description="Artifacts a verifier should observe to justify pass/fail (human commentary).",
    )
    targeted_failure_modes: list[str] = Field(
        default_factory=list,
        description=(
            "Failure patterns this task is designed to expose (free strings for now). The "
            "canonical vocabulary is co-owned with Darrel (Attribution) and should stay "
            "consistent with the Judge's failure_category; not yet ratified."
        ),
    )
    verifier_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Ids of the deterministic verifiers that judge this task (resolved in verifiers/)."
        ),
    )
    severity: Severity = Field(
        default=Severity.MEDIUM,
        description="How bad it is if an agent fails this task; seeds downstream failure severity.",
    )
    difficulty: Difficulty | None = Field(
        default=None,
        description=(
            "Optional calibration label (easy/medium/hard) for suite balance and reporting; "
            "unset means not yet calibrated. Does not affect pass/fail."
        ),
    )
    requires_escalation: bool = Field(
        default=False,
        description=(
            "Whether a correct run must escalate to a human (via the escalate_case tool) rather "
            "than resolve the case itself. When true, the verifier treats a run that issues no "
            "refund AND records no escalation as a failure — this is what distinguishes a "
            "missing-info / must-escalate task from a plain no-refund decline. Consumed by the "
            "RefundPolicyVerifier's escalation check (owned with Karan); a task that sets this "
            "must offer escalate_case in available_tools (enforced by the task-validity rubric)."
        ),
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Free-form harness keys: fixture_script, user_message, positive_sibling_tasks, "
            "design_owner. user_message becomes first-class once multi-turn shape settles."
        ),
    )

    @field_validator("available_tools", "available_docs", "verifier_ids", "targeted_failure_modes")
    @classmethod
    def _no_duplicate_entries(cls, value: list[str], info: ValidationInfo) -> list[str]:
        seen: set[str] = set()
        dupes: set[str] = set()
        for entry in value:
            if entry in seen:
                dupes.add(entry)
            seen.add(entry)
        if dupes:
            raise ValueError(f"{info.field_name} contains duplicate entries: {sorted(dupes)}")
        return value

    @model_validator(mode="after")
    def _docs_must_be_resolvable(self) -> TaskSpec:
        """If a task names doc ids, they must be resolvable — either via a
        ``docs_fixture`` or inline ``initial_state['docs']`` — so the loader
        never silently drops referenced docs."""
        if self.available_docs and self.docs_fixture is None and "docs" not in self.initial_state:
            raise ValueError(
                "available_docs is set but there is no docs_fixture and no inline "
                "initial_state['docs']; the referenced doc ids cannot be resolved"
            )
        return self
