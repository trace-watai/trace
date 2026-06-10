"""Verifier and attribution (judge) result contracts.

OWNERSHIP BOUNDARY:
- Deterministic verifier interface/result is authored by TRA-16 (Karan).
- Attribution/judge output schema is authored by TRA-10 (Darrel); the failure
  taxonomy is TRA-13.
These models are the *trace-side* contract so traces can carry/reference results
today. They are forward-compatible (``extra='allow'``) and field names match the
canonical shapes in Notion → "Trace Schema & Data Formats".
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Evidence(BaseModel):
    """A renderable evidence unit. Design rule: the dashboard must render evidence
    without custom parsing, so keep it to flat, typed pointers + values."""

    model_config = ConfigDict(extra="allow")

    kind: str = Field(..., description="e.g. 'step_ref' | 'state_path' | 'doc_span' | 'tool_arg' | 'text'.")
    step_id: int | None = Field(None, description="Trajectory step this evidence points at, if any.")
    pointer: str | None = Field(None, description="JSON-path / document id / arg name the value came from.")
    value: Any | None = None
    note: str | None = None


class VerifierCheck(BaseModel):
    """One deterministic check within a verifier pack."""

    model_config = ConfigDict(extra="allow")

    check_id: str
    passed: bool
    severity: str | None = Field(None, description="'hard' (release-blocking) | 'warning'.")
    detail: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)


class VerifierResult(BaseModel):
    """Deterministic pass/fail decision. The verifier is the source of truth;
    the judge only explains (evidence-first diagnosis)."""

    model_config = ConfigDict(extra="allow")

    verifier_passed: bool
    failed_checks: list[str] = Field(default_factory=list, description="check_ids that failed (quick index).")
    checks: list[VerifierCheck] = Field(default_factory=list)
    expected: dict[str, Any] = Field(default_factory=dict)
    actual: dict[str, Any] = Field(default_factory=dict)
    evidence: list[Evidence] = Field(default_factory=list)


class AttributionResult(BaseModel):
    """Judge/attribution output. Runs ONLY after a verifier result exists and never
    overrides it. Step fields use the TRACE attribution vocabulary.

    Authoritative schema: TRA-10. Taxonomy for ``failure_category``: TRA-13.
    """

    model_config = ConfigDict(extra="allow")

    task_success: bool
    failure_category: str | None = Field(None, description="Taxonomy bucket; owned by TRA-13.")
    # Attribution step vocabulary (earliest → symptom):
    first_suspicious_step: int | None = None
    first_unrecoverable_step: int | None = Field(
        None, description="The critical failure step: earliest point the run became unrecoverable."
    )
    missed_recovery_step: int | None = None
    harmful_action_step: int | None = None
    visible_failure_step: int | None = Field(None, description="Where the failure first became user-visible.")
    causal_explanation: str = ""
    evidence: list[Evidence] = Field(default_factory=list)
    suggested_guardrail: str | None = None
    regression_test_idea: str | None = None
    confidence: float | None = Field(None, ge=0.0, le=1.0)
