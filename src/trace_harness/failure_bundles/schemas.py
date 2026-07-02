"""Schemas for failure cards and repair packages.

A *failure card* is the human-readable artifact: what broke, where, how bad,
with evidence. A *repair package* is the engineering artifact: concrete
controls that would prevent recurrence, each with an installation point and
a tradeoff statement. Both are generated from (task, trace, verifier
result, attribution) — never from vibes.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from trace_harness.tasks.schemas import Severity
from trace_harness.verifiers.base import EvidenceItem

FAILURE_CARD_SCHEMA_VERSION = "0.2.0"
REPAIR_PACKAGE_SCHEMA_VERSION = "0.2.0"


class FailureCard(BaseModel):
    """Human-readable summary of one verified failure."""

    schema_version: str = FAILURE_CARD_SCHEMA_VERSION
    run_id: str
    task_id: str
    title: str
    summary: str
    # How the run itself ended + verifier verdict, e.g.
    # "completed (final_answer, 7 steps); verifier FAILED (3 checks)".
    task_result: str
    severity: Severity
    root_cause: str
    # Coarse failure categories that contributed (primary first), sourced from
    # attribution. Surfaces contributing_failure_categories as a first-class
    # field rather than burying them in metadata.
    contributing_failures: list[str] = Field(default_factory=list)
    # Step numbers directly implicated in the failure, drawn from failed
    # verifier checks — lets readers jump straight to the relevant trace lines.
    step_ids: list[int] = Field(default_factory=list)
    visible_symptoms: list[str] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    causal_explanation: str
    # What actually got out: money moved, durable records created, customers
    # affected. Scope, not adjectives.
    blast_radius: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class RepairControl(BaseModel):
    """One concrete control that would prevent this failure class."""

    name: str
    # Where in the codebase/config the control installs — a real seam,
    # e.g. "SupportEnvironment.execute, pre-dispatch for issue_refund".
    installation_point: str
    # The exact check the control performs, stated deterministically.
    check: str
    behavior_on_failure: str
    # Measurable outcome if the control is installed: what rate/class of
    # failure it eliminates. Distinct from why_it_prevents_recurrence, which
    # is the causal claim; this is the observable engineering impact.
    expected_impact: str
    why_it_prevents_recurrence: str
    risk_or_tradeoff: str
    # P0 (do before next release) .. P3 (opportunistic).
    priority: str
    linked_verifier_checks: list[str] = Field(default_factory=list)


class RepairPackage(BaseModel):
    """Structured engineering recommendation derived from one failure."""

    schema_version: str = REPAIR_PACKAGE_SCHEMA_VERSION
    run_id: str
    task_id: str
    summary: str
    controls: list[RepairControl] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
