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

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_serializer,
    model_validator,
)

TASK_SCHEMA_VERSION = "0.6.0"  # 0.6.0: declared claim; 0.5.0: posture; 0.4.0: expected_action


class ExpectedRefund(StrEnum):
    """The refund state a correct run must leave for the customer.

    ``none`` is a positive assertion — a *clean decline* — not "unspecified".
    A task that does not care about the refund outcome simply omits
    ``expected_action.refund`` rather than setting ``none``.
    """

    CASH = "cash"
    STORE_CREDIT = "store_credit"
    NONE = "none"


class EscalationPosture(StrEnum):
    """Whether a correct run escalates.

    ``conditional`` exists because two tasks can have identical order fields
    and opposite correct answers. The only thing separating "escalated when a
    clean decline was correct" from "escalated correctly on a claim nothing
    could confirm" is what the customer said, which the order record cannot
    settle. The task declares which case it is and names the condition.
    """

    REQUIRED = "required"
    FORBIDDEN = "forbidden"
    CONDITIONAL = "conditional"


class EscalationCondition(StrEnum):
    """What makes escalation warranted for a ``conditional`` task.

    Each names a claim the customer makes that the order record does not
    confirm. An agent cannot resolve such a claim on its own, so escalating is
    the correct move and declining outright is not.
    """

    UNVERIFIABLE_APPROVAL_CLAIM = "unverifiable_approval_claim"
    UNVERIFIABLE_OUTAGE_CLAIM = "unverifiable_outage_claim"


class EscalationExpectation(BaseModel):
    """The escalation posture a correct run must satisfy.

    ``claim_made`` records whether the customer makes the claim the condition
    names. The task author knows what the message says, and the verifier used
    to infer it by matching the message against word lists, which misreads
    requests, questions and negations (TRA-79). Unset means undeclared, and the
    verifier then falls back to that matcher, so tasks written before 0.6.0 and
    run artifacts already on disk keep their old behavior.
    """

    model_config = ConfigDict(extra="forbid")

    posture: EscalationPosture
    condition: EscalationCondition | None = Field(
        default=None,
        description="Required for 'conditional'; rejected for the other two postures.",
    )
    claim_made: bool | None = Field(
        default=None,
        description=(
            "Whether the customer makes the claim named by 'condition'. Only valid on "
            "'conditional'. Unset means undeclared, and the verifier matches the message instead."
        ),
    )

    @model_validator(mode="after")
    def _condition_matches_posture(self) -> EscalationExpectation:
        if self.posture is EscalationPosture.CONDITIONAL and self.condition is None:
            raise ValueError("a conditional escalation posture must name its condition")
        if self.posture is not EscalationPosture.CONDITIONAL and self.condition is not None:
            raise ValueError(f"a {self.posture.value} escalation posture cannot carry a condition")
        if self.posture is not EscalationPosture.CONDITIONAL and self.claim_made is not None:
            raise ValueError(
                f"a {self.posture.value} escalation posture cannot declare a customer claim"
            )
        return self

    @model_serializer(mode="wrap")
    def _omit_an_undeclared_claim(self, handler: Any) -> Any:
        """Leave ``claim_made`` out of the dump when it is unset.

        Main forbids extra keys, so writing ``"claim_made": null`` into every
        run of every escalation task would make those runs unreadable to a
        tree without this field. Omitting the unset value keeps an undeclared
        task's artifacts byte-identical to what main writes.
        """
        data = handler(self)
        if isinstance(data, dict) and self.claim_made is None:
            data.pop("claim_made", None)
        return data


class ExpectedAction(BaseModel):
    """The remedy / final-action contract a correct run must satisfy.

    This is the positive counterpart to ``forbidden_actions``: it lets a task
    assert *what should have happened*, so the verifier can prove the expected
    action was completed rather than only that nothing forbidden occurred
    (TRA-80). Every field is optional — a task asserts only the dimensions
    that define correctness for its branch:

    - ``refund`` — the expected final refund state. ``cash``/``store_credit``
      requires exactly one refund of that type for the customer's order;
      ``none`` requires that no refund exists (a clean decline). This catches
      an allowed refund that was omitted or swapped for the wrong allowed type.
    - ``escalation`` — whether an escalation is expected. ``false`` flags an
      unexpected escalation on a case that should have been resolved or
      cleanly declined without one; ``true`` asserts an escalation is present.

    ``extra="forbid"`` so a typo'd key fails at load, matching the rest of the
    task schema.
    """

    model_config = ConfigDict(extra="forbid")

    refund: ExpectedRefund | None = Field(
        default=None,
        description=(
            "Expected final refund state: 'cash'/'store_credit' (exactly one refund of that "
            "type must exist) or 'none' (a clean decline — no refund may exist). Omit if the "
            "task does not constrain the refund outcome."
        ),
    )
    escalation: EscalationExpectation | None = Field(
        default=None,
        description=(
            "Whether an escalation is expected, as a posture. Omit to leave escalation "
            "unconstrained here (see requires_escalation)."
        ),
    )

    @field_validator("escalation", mode="before")
    @classmethod
    def accept_the_legacy_boolean(cls, value: Any) -> Any:
        """Read a pre-0.5.0 ``escalation: true/false`` as the posture it meant.

        Run artifacts on disk are not versioned forward. Retained control
        evidence under ``fixtures/controls/evidence/`` is sha256-pinned in the
        control library, so those task specs cannot be rewritten without
        re-promoting the control, and a developer's own ``runs/`` directory
        holds more of them. Rejecting the boolean makes ``verify``,
        ``attribute`` and ``bundle`` fail on every one of those runs.

        ``true`` meant an escalation must be present and ``false`` meant one
        must not, which are exactly ``required`` and ``forbidden``. Only the
        boolean is coerced; anything else is left for normal validation to
        reject.
        """
        if value is True:
            return {"posture": EscalationPosture.REQUIRED}
        if value is False:
            return {"posture": EscalationPosture.FORBIDDEN}
        return value


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
    expected_action: ExpectedAction | None = Field(
        default=None,
        description=(
            "Optional positive remedy contract: what a correct run must actually do (expected "
            "refund type / decline, expected escalation state). When set, the RefundPolicyVerifier "
            "asserts the expected action was completed — not just that nothing forbidden happened "
            "(TRA-80). Omitted on tasks that only assert absence of violations."
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
