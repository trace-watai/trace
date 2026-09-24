"""Controls as data: typed, installable guardrail instances (TRA-87).

A *control* is a guardrail that a repair package prescribed and a human (or,
later, the control library) decided to install. Until this module existed a
control was a bare Python function imported by name in ``cli.py``; the set of
installable controls could only grow by editing source, nothing recorded which
failure earned a control, and a block in the trace was an error string.

This module makes a control a value:

- :class:`ControlInstance` names the guardrail implementation it uses
  (``guardrail_ref``, a key in :data:`GUARDRAIL_REGISTRY`), the policy rules it
  enforces, what it does on failure, and where it came from.
- :data:`GUARDRAIL_REGISTRY` is the only place a ``guardrail_ref`` becomes a
  function, and records which policy rules each guardrail reads. Unknown refs,
  and a ``rule_ref`` that doesn't match what its guardrail reads, fail at
  install time, never at dispatch time.
- :func:`reference_controls` returns the controls the repository ships today,
  which is exactly what ``replay --apply-control`` installs by default.

Installing happens on the environment (``SupportEnvironment.install_control``)
so the installed set is explicit, inspectable state rather than a list of
anonymous callables. The guardrail functions themselves stay in
``environment/guardrails.py`` and know nothing about this module.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from trace_harness.environment.guardrails import (
    DEPRECATED_POLICY_CITATION_RULE_KEYS,
    REFUND_POLICY_RULE_KEYS,
    REQUIRED_ESCALATION_RULE_KEYS,
    UNAUTHORIZED_CASH_REFUND_RULE_KEYS,
    FinalAnswerGuardrailFn,
    deprecated_policy_citation_guardrail,
    final_answer_state_grounding_guardrail,
    required_escalation_guardrail,
    ticket_outage_claim_guardrail,
    unauthorized_cash_refund_guardrail,
    unauthorized_refund_guardrail,
)
from trace_harness.environment.state import SupportState
from trace_harness.environment.tools import ToolResult
from trace_harness.models.base import ToolCall

CONTROL_SCHEMA_VERSION = "0.1.0"

GuardrailFn = Callable[[ToolCall, SupportState], ToolResult | None]


@dataclass(frozen=True)
class RegisteredGuardrail:
    """A guardrail implementation and the policy rules it reads.

    A ``ControlInstance``'s ``rule_ref`` must match ``rule_source`` and
    ``rule_keys`` exactly to install: a control may neither claim rules its
    guardrail ignores nor leave out rules it enforces.
    """

    fn: GuardrailFn | FinalAnswerGuardrailFn
    rule_source: str
    rule_keys: frozenset[str]
    # Replay classification describes executable coverage, not repair-package prose.
    checks_covered: frozenset[str] = frozenset()
    rule_kind: Literal["prohibition", "requirement"] | None = None
    # Where the environment runs it. A pre-call guardrail sees a tool call
    # before dispatch; a final-answer guardrail sees the answer before the run
    # accepts it (#193) and receives the task, since the escalation rule reads
    # the task's posture and message.
    seam: Literal["pre_call", "final_answer"] = "pre_call"


# guardrail_ref -> implementation. New guardrails register here; nothing else
# imports them by name.
GUARDRAIL_REGISTRY: dict[str, RegisteredGuardrail] = {
    "unauthorized_cash_refund_guardrail": RegisteredGuardrail(
        fn=unauthorized_cash_refund_guardrail,
        rule_source="current_policy_doc",
        rule_keys=UNAUTHORIZED_CASH_REFUND_RULE_KEYS,
        checks_covered=frozenset({"unauthorized_cash_refund"}),
        rule_kind="prohibition",
    ),
    # #194. The first entry stays exactly as registered, because the pinned
    # control library installs it and a control only installs when its
    # rule_ref matches what its guardrail reads. Store credit is covered by a
    # separate combined guardrail instead of by widening that one.
    "unauthorized_refund_guardrail": RegisteredGuardrail(
        fn=unauthorized_refund_guardrail,
        rule_source="current_policy_doc",
        rule_keys=REFUND_POLICY_RULE_KEYS,
        checks_covered=frozenset({"unauthorized_cash_refund", "unauthorized_store_credit"}),
        rule_kind="prohibition",
    ),
    "deprecated_policy_citation_guardrail": RegisteredGuardrail(
        fn=deprecated_policy_citation_guardrail,
        rule_source="doc_status",
        rule_keys=DEPRECATED_POLICY_CITATION_RULE_KEYS,
        checks_covered=frozenset({"deprecated_policy_treated_as_authoritative"}),
        rule_kind="prohibition",
    ),
    "ticket_outage_claim_guardrail": RegisteredGuardrail(
        fn=ticket_outage_claim_guardrail,
        rule_source="order_record",
        rule_keys=frozenset({"documented_outage_near_purchase"}),
        checks_covered=frozenset({"ticket_outage_claim_unsupported"}),
        rule_kind="prohibition",
    ),
    "final_answer_state_grounding_guardrail": RegisteredGuardrail(
        fn=final_answer_state_grounding_guardrail,
        rule_source="final_state",
        rule_keys=frozenset({"refunds"}),
        checks_covered=frozenset({"final_answer_inconsistent_with_state"}),
        rule_kind="prohibition",
        seam="final_answer",
    ),
    "required_escalation_guardrail": RegisteredGuardrail(
        fn=required_escalation_guardrail,
        rule_source="task_expectation",
        rule_keys=REQUIRED_ESCALATION_RULE_KEYS,
        checks_covered=frozenset({"required_escalation_missing"}),
        rule_kind="requirement",
        seam="final_answer",
    ),
}


class UnknownGuardrailError(ValueError):
    """A ``ControlInstance`` names a ``guardrail_ref`` that is not registered."""


class RuleRefMismatchError(ValueError):
    """A ``ControlInstance``'s ``rule_ref`` differs from what its guardrail reads."""


class RuleRef(BaseModel):
    """Which policy rules the control enforces, and where it reads them from."""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(description="where the rules are read, e.g. 'current_policy_doc'")
    rules: list[str] = Field(
        default_factory=list, description="rule keys, e.g. metadata.rules names"
    )


class BehaviorOnFailure(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["block"] = "block"


class ControlProvenance(BaseModel):
    """The failure that earned this control. Both fields are optional so an
    authored (not earned) control can still be represented."""

    model_config = ConfigDict(extra="forbid")

    run_id: str | None = None
    repair_control: str | None = Field(
        default=None, description="RepairControl.name in the originating repair package"
    )


class ControlInstance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = CONTROL_SCHEMA_VERSION
    control_id: str
    guardrail_ref: str
    rule_ref: RuleRef
    behavior_on_failure: BehaviorOnFailure = Field(default_factory=BehaviorOnFailure)
    provenance: ControlProvenance = Field(default_factory=ControlProvenance)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ControlConflictError(ValueError):
    """Two active controls read the same rules but disagree on what to do."""


def find_conflict(
    candidate: ControlInstance, installed: list[ControlInstance]
) -> ControlInstance | None:
    """The installed control ``candidate`` contradicts, if any.

    Two controls conflict when they name the same ``guardrail_ref`` and the
    same rules but different ``behavior_on_failure``. Installing both would
    leave the outcome decided by ordering, which is not a decision anyone made.
    Same guardrail and same behavior is redundant rather than contradictory, so
    it is allowed; #147 owns ordering and this owns disagreement.
    """
    for other in installed:
        if other.guardrail_ref != candidate.guardrail_ref:
            continue
        if other.rule_ref.source != candidate.rule_ref.source:
            continue
        if set(other.rule_ref.rules) != set(candidate.rule_ref.rules):
            continue
        if other.behavior_on_failure != candidate.behavior_on_failure:
            return other
    return None


def resolve_guardrail(guardrail_ref: str) -> RegisteredGuardrail:
    """Return the registry entry for ``guardrail_ref`` or raise at install time."""
    try:
        return GUARDRAIL_REGISTRY[guardrail_ref]
    except KeyError:
        raise UnknownGuardrailError(
            f"unknown guardrail_ref {guardrail_ref!r}; registered: {sorted(GUARDRAIL_REGISTRY)}"
        ) from None


def resolve_control(instance: ControlInstance) -> GuardrailFn | FinalAnswerGuardrailFn:
    """The guardrail ``instance`` installs, checked now rather than at dispatch.

    Raises ``UnknownGuardrailError`` for an unregistered ``guardrail_ref`` and
    ``RuleRefMismatchError`` unless ``rule_ref`` names exactly the source and
    rule keys the guardrail reads (key order doesn't matter).

    ``rule_ref`` deliberately restates what the registry already records. A
    control is data that outlives whatever installed it, so it has to describe
    itself without the registry at hand. Requiring an exact match is what
    catches a guardrail quietly changing which rules it reads: stored controls
    that still name the old rules stop installing instead of enforcing
    something other than what they say.
    """
    registered = resolve_guardrail(instance.guardrail_ref)
    claimed = set(instance.rule_ref.rules)
    if instance.rule_ref.source != registered.rule_source or claimed != registered.rule_keys:
        raise RuleRefMismatchError(
            f"control {instance.control_id!r}: rule_ref "
            f"{instance.rule_ref.source}:{sorted(claimed)} does not match guardrail "
            f"{instance.guardrail_ref!r}, which reads "
            f"{registered.rule_source}:{sorted(registered.rule_keys)}"
        )
    return registered.fn


# The controls the repository ships. ``replay --apply-control`` installs all of
# these unless ``--control`` narrows the set. ``provenance.run_id`` is None
# because these are reference controls, not ones earned from a specific run;
# the control library (TRA-93) fills that in for earned controls.
REFUND_WINDOW_CONTROL_ID = "ctl_refund_window_v1"


def reference_controls() -> list[ControlInstance]:
    return [
        ControlInstance(
            control_id=REFUND_WINDOW_CONTROL_ID,
            guardrail_ref="unauthorized_cash_refund_guardrail",
            rule_ref=RuleRef(
                source="current_policy_doc",
                rules=["cash_refund_window_days", "manager_approval_extends_cash_to_days"],
            ),
            provenance=ControlProvenance(repair_control="deterministic_pre_call_refund_guardrail"),
        )
    ]


def _control(control_id: str, guardrail_ref: str, repair_control: str) -> ControlInstance:
    registered = GUARDRAIL_REGISTRY[guardrail_ref]
    return ControlInstance(
        control_id=control_id,
        guardrail_ref=guardrail_ref,
        rule_ref=RuleRef(source=registered.rule_source, rules=sorted(registered.rule_keys)),
        provenance=ControlProvenance(repair_control=repair_control),
    )


def control_catalogue() -> list[ControlInstance]:
    """Every control the repository can install: the reference set plus #194's.

    The reference set is what ``replay --apply-control`` installs by default
    and what the materializer installs to predict an artifact's replay mode.
    It stays one control on purpose, since widening it changes the replay
    label of every artifact and every pinned expectation built on one. The
    others are selected by id with ``--control``, and per-control validation
    finds them by the repair control they materialize.
    """
    return [
        *reference_controls(),
        _control(
            "ctl_refund_policy_v2",
            "unauthorized_refund_guardrail",
            "deterministic_pre_call_refund_guardrail",
        ),
        _control(
            "ctl_policy_source_v1",
            "deprecated_policy_citation_guardrail",
            "current_policy_source_precedence",
        ),
        _control(
            "ctl_ticket_grounding_v1",
            "ticket_outage_claim_guardrail",
            "ticket_claim_grounding_check",
        ),
        _control(
            "ctl_final_answer_grounding_v1",
            "final_answer_state_grounding_guardrail",
            "final_answer_state_grounding_check",
        ),
        _control(
            "ctl_required_escalation_v1",
            "required_escalation_guardrail",
            "required_escalation_enforcement",
        ),
    ]


def select_controls(control_ids: list[str] | None) -> list[ControlInstance]:
    """Controls from the catalogue by id, or the reference set when ``None``.

    Raises ``ValueError`` for an id that is not in the catalogue, so a typo
    fails before any run starts.
    """
    if control_ids is None:
        return reference_controls()
    available = control_catalogue()
    by_id = {c.control_id: c for c in available}
    unknown = [cid for cid in control_ids if cid not in by_id]
    if unknown:
        raise ValueError(f"unknown control id(s) {unknown}; available: {sorted(by_id)}")
    return [by_id[cid] for cid in control_ids]


# Which prescribed repair controls (RepairControl.name, see
# failure_bundles/generator.py::_CONTROL_BUILDERS) have an executable guardrail
# today. ``None`` means the repair package can prescribe it but nothing can
# install it yet; per-control validation (TRA-92) reports those as
# ``skipped: not_materializable`` rather than pretending.
MATERIALIZABLE_REPAIR_CONTROLS: dict[str, str | None] = {
    "deterministic_pre_call_refund_guardrail": "unauthorized_cash_refund_guardrail",
    "current_policy_source_precedence": "deprecated_policy_citation_guardrail",
    "ticket_claim_grounding_check": "ticket_outage_claim_guardrail",
    "final_answer_state_grounding_check": "final_answer_state_grounding_guardrail",
    "required_escalation_enforcement": "required_escalation_guardrail",
    # A CI-side control, not an environment guardrail; the regression
    # collector (issue #161) is what makes it real.
    "regression_test_ci_gate": None,
    # Detection rather than a guardrail: an omitted remedy cannot be caused by
    # blocking something, so no pre-dispatch hook can materialize this.
    "expected_action_contract_check": None,
    "escalation_discipline_check": None,
    "retrieval_before_action_check": None,
}


def guardrail_ref_for_repair_control(repair_control_name: str) -> str | None:
    """``guardrail_ref`` that materializes a prescribed control, or ``None``."""
    return MATERIALIZABLE_REPAIR_CONTROLS.get(repair_control_name)
