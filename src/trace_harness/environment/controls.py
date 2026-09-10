"""Controls as data: typed, installable guardrail instances (TRA-91).

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
  function. Unknown refs fail at install time, never at dispatch time.
- :func:`reference_controls` returns the controls the repository ships today,
  which is exactly what ``replay --apply-control`` installs by default.

Installing happens on the environment (``SupportEnvironment.install_control``)
so the installed set is explicit, inspectable state rather than a list of
anonymous callables. The guardrail functions themselves stay in
``environment/guardrails.py`` and know nothing about this module.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, Field

from trace_harness.environment.guardrails import unauthorized_cash_refund_guardrail
from trace_harness.environment.state import SupportState
from trace_harness.environment.tools import ToolResult
from trace_harness.models.base import ToolCall

CONTROL_SCHEMA_VERSION = "0.1.0"

GuardrailFn = Callable[[ToolCall, SupportState], ToolResult | None]

# guardrail_ref -> implementation. Seeded with the one guardrail the repository
# ships. New guardrails register here; nothing else imports them by name.
GUARDRAIL_REGISTRY: dict[str, GuardrailFn] = {
    "unauthorized_cash_refund_guardrail": unauthorized_cash_refund_guardrail,
}


class UnknownGuardrailError(ValueError):
    """A ``ControlInstance`` names a ``guardrail_ref`` that is not registered."""


class RuleRef(BaseModel):
    """Which policy rules the control enforces, and where it reads them from."""

    source: str = Field(description="where the rules are read, e.g. 'current_policy_doc'")
    rules: list[str] = Field(
        default_factory=list, description="rule keys, e.g. metadata.rules names"
    )


class BehaviorOnFailure(BaseModel):
    action: Literal["block"] = "block"


class ControlProvenance(BaseModel):
    """The failure that earned this control. Both fields are optional so an
    authored (not earned) control can still be represented."""

    run_id: str | None = None
    repair_control: str | None = Field(
        default=None, description="RepairControl.name in the originating repair package"
    )


class ControlInstance(BaseModel):
    schema_version: str = CONTROL_SCHEMA_VERSION
    control_id: str
    guardrail_ref: str
    rule_ref: RuleRef
    behavior_on_failure: BehaviorOnFailure = Field(default_factory=BehaviorOnFailure)
    provenance: ControlProvenance = Field(default_factory=ControlProvenance)
    metadata: dict[str, Any] = Field(default_factory=dict)


def resolve_guardrail(guardrail_ref: str) -> GuardrailFn:
    """Return the implementation for ``guardrail_ref`` or raise at install time."""
    try:
        return GUARDRAIL_REGISTRY[guardrail_ref]
    except KeyError:
        raise UnknownGuardrailError(
            f"unknown guardrail_ref {guardrail_ref!r}; registered: {sorted(GUARDRAIL_REGISTRY)}"
        ) from None


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


def select_controls(control_ids: list[str] | None) -> list[ControlInstance]:
    """Reference controls filtered to ``control_ids`` (all when ``None``).

    Raises ``UnknownGuardrailError``'s sibling, ``ValueError``, for an id that
    is not a reference control, so a typo fails before any run starts.
    """
    available = reference_controls()
    if control_ids is None:
        return available
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
    "current_policy_source_precedence": None,
    "ticket_claim_grounding_check": None,
    "final_answer_state_grounding_check": None,
    "required_escalation_enforcement": None,
    # A CI-side control, not an environment guardrail; the regression
    # collector (issue #161) is what makes it real.
    "regression_test_ci_gate": None,
}


def guardrail_ref_for_repair_control(repair_control_name: str) -> str | None:
    """``guardrail_ref`` that materializes a prescribed control, or ``None``."""
    return MATERIALIZABLE_REPAIR_CONTROLS.get(repair_control_name)
