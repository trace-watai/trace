"""Schemas for failure cards and repair packages.

A *failure card* is the human-readable artifact: what broke, where, how bad,
with evidence. A *repair package* is the engineering artifact: concrete
controls that would prevent recurrence, each with an installation point and
a tradeoff statement. Both are generated from (task, trace, verifier
result, attribution) — never from vibes.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from trace_harness.attribution.schemas import FailureCategory
from trace_harness.tasks.schemas import Severity
from trace_harness.tracing.artifact_store import safe_run_dir_name
from trace_harness.verifiers.base import EvidenceItem

# 0.5.0: bundle_key and occurrences (#211); 0.4.0: BlastRadius gained escalation_count
FAILURE_CARD_SCHEMA_VERSION = "0.5.0"
REPAIR_PACKAGE_SCHEMA_VERSION = "0.3.0"
BUNDLE_REF_SCHEMA_VERSION = "0.1.0"


class BlastRadius(BaseModel):
    """Structured scope of external impact from a failed run.

    All fields are computed from actual final state. The dashboard renders each field separately.
    ``summary`` is a human readable fallback for plaintext contexts (e.g. PR comments, CLI output).
    """

    refund_count: int = 0
    refund_total_usd: float = 0.0
    ticket_count: int = 0
    escalation_count: int = 0
    customers_affected: list[str] = Field(default_factory=list)
    # Pre-formatted summary for plain-text display (PR comments, CLI output).
    summary: str = ""


class ControlPriority(StrEnum):
    """Ordered urgency levels for repair controls.

    P0 — do before the next release.
    P1 — do in the upcoming development cycle.
    P2 — schedule soon.
    P3 — optional improvements.
    """

    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class BundleOccurrence(BaseModel):
    """One failing run that a failure card covers (0.5.0, #211).

    The first occurrence is the run the card, repair package and regression
    artifact were generated from. Every later one is a reproduction of it, a
    run whose failure produced the same bundle key. ``provider``, ``model`` and
    ``seed`` come from that run's ``run_config.json`` so a sweep's card can say
    which configurations hit the failure.
    """

    run_id: str
    task_id: str
    provider: str | None = None
    model: str | None = None
    seed: int | None = None


class FailureCard(BaseModel):
    """Human-readable summary of one verified failure, and of every run that repeated it."""

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
    # attribution. Typed as FailureCategory so the dashboard has a 'closed
    # vocabulary' of categories to branch on.
    contributing_failures: list[FailureCategory] = Field(default_factory=list)
    # Step numbers directly implicated in the failure, drawn from failed
    # verifier checks — lets readers jump straight to the relevant trace lines.
    step_ids: list[int] = Field(default_factory=list)
    visible_symptoms: list[str] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    causal_explanation: str
    # Structured scope of external impact: money moved, records created,
    # customers affected — computed from final state, never adjectives.
    blast_radius: BlastRadius
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Root-cause identity (0.5.0). None on cards written before 0.5.0, which
    # were never matched by key and keep describing their own run alone. See
    # failure_bundles.generator.bundle_key for how the key is formed.
    bundle_key: str | None = None
    # Every run this card covers, in the order they were bundled, the card's
    # own run first. Empty on cards written before 0.5.0.
    occurrences: list[BundleOccurrence] = Field(default_factory=list)

    @model_validator(mode="after")
    def _occurrences_start_at_this_run(self) -> FailureCard:
        run_ids = [occurrence.run_id for occurrence in self.occurrences]
        if len(run_ids) != len(set(run_ids)):
            raise ValueError("a run is listed more than once in occurrences")
        if run_ids and run_ids[0] != self.run_id:
            raise ValueError("the first occurrence must be the run the card was generated from")
        return self


class BundleRef(BaseModel):
    """What a reproduction's run directory holds in place of its own bundle (#211).

    Written as ``bundle_ref.json`` when a run's bundle key matched an existing
    card. The card, repair package and regression artifact stay in the
    directory of ``canonical_run_id``, the key's first occurrence, whose card
    lists this run among its occurrences.
    """

    schema_version: str = BUNDLE_REF_SCHEMA_VERSION
    run_id: str
    task_id: str
    bundle_key: str
    canonical_run_id: str

    @field_validator("canonical_run_id")
    @classmethod
    def _canonical_is_a_sibling(cls, value: str) -> str:
        return safe_run_dir_name(value)


class RepairControl(BaseModel):
    """One concrete control that would prevent this failure class."""

    name: str
    # Where in the codebase/config the control installs — a real seam,
    # e.g. "SupportEnvironment.execute, pre-dispatch for issue_refund".
    installation_point: str
    # The exact check the control performs, stated deterministically.
    check: str
    behavior_on_failure: str
    # Measurable outcome if the control is installed: what class of
    # failure it eliminates (distinct from why_it_prevents_recurrence,
    # which is the causal claim).
    expected_impact: str
    why_it_prevents_recurrence: str
    risk_or_tradeoff: str
    priority: ControlPriority
    linked_verifier_checks: list[str] = Field(default_factory=list)


class RepairPackage(BaseModel):
    """Structured engineering recommendation derived from one failure."""

    schema_version: str = REPAIR_PACKAGE_SCHEMA_VERSION
    run_id: str
    task_id: str
    summary: str
    controls: list[RepairControl] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
