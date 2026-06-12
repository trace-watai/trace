"""Verifier base contract and result schemas.

The verifier is the *authority* on pass/fail. It consumes a finished run's
inputs and outputs — task, trace, final state — and returns a structured
:class:`VerifierResult` with evidence, never a bare boolean. LLM judges may
later classify and explain, but release-blocking correctness stays
deterministic (see docs/verifier_philosophy.md).

Contract notes
    - ``final_state`` is passed as a plain dict (exactly what
      ``final_state.json`` contains) so verification works identically on a
      live run object and a reloaded artifact. Domain verifiers parse it
      into their typed state internally.
    - A verifier must degrade gracefully: missing trace events become
      warnings, not crashes. A partial trace from a crashed run should still
      yield whatever checks are computable.
    - Checks that pass are silent; checks that cannot run are warnings;
      checks that fail carry evidence with step provenance.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from trace_harness.tasks.schemas import Severity, TaskSpec, max_severity
from trace_harness.tracing.events import TraceEvent

VERIFIER_RESULT_SCHEMA_VERSION = "0.1.0"


class EvidenceItem(BaseModel):
    """One piece of evidence supporting a check outcome.

    ``kind`` is a short machine-usable tag (e.g. ``order_record``,
    ``policy_rule``, ``reasoning_quote``); ``data`` carries the raw facts so
    the dashboard can render them without re-deriving anything.
    """

    kind: str
    description: str
    step_ids: list[int] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)


class FailedCheck(BaseModel):
    """One deterministic check that failed, with everything needed to act on it."""

    check_id: str
    message: str
    expected: str
    actual: str
    step_ids: list[int] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    severity: Severity = Severity.HIGH
    blocks_release: bool = True


class VerifierResult(BaseModel):
    """The verdict for one run from one verifier (or a merge of several)."""

    schema_version: str = VERIFIER_RESULT_SCHEMA_VERSION
    verifier_id: str
    run_id: str
    passed: bool
    failed_checks: list[FailedCheck] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    # Highest severity among failed checks; None when passed.
    severity: Severity | None = None
    blocks_release: bool = False
    # Run-level evidence not tied to a single check (e.g. retrieval provenance).
    evidence: list[EvidenceItem] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


def build_result(
    *,
    verifier_id: str,
    run_id: str,
    failed_checks: list[FailedCheck],
    warnings: list[str],
    evidence: list[EvidenceItem] | None = None,
    metadata: dict[str, Any] | None = None,
) -> VerifierResult:
    """Assemble a result, deriving passed/severity/blocks_release from the checks."""
    return VerifierResult(
        verifier_id=verifier_id,
        run_id=run_id,
        passed=not failed_checks,
        failed_checks=failed_checks,
        warnings=warnings,
        severity=max_severity(check.severity for check in failed_checks),
        blocks_release=any(check.blocks_release for check in failed_checks),
        evidence=evidence or [],
        metadata=metadata or {},
    )


def merge_verifier_results(results: list[VerifierResult]) -> VerifierResult:
    """Merge results from multiple verifiers into one composite verdict.

    Used when a task lists several ``verifier_ids``. Check ids must already
    be globally unique (convention: prefix with the verifier's domain).
    """
    if not results:
        raise ValueError("cannot merge zero verifier results")
    if len(results) == 1:
        return results[0]
    failed = [check for result in results for check in result.failed_checks]
    return VerifierResult(
        verifier_id="+".join(result.verifier_id for result in results),
        run_id=results[0].run_id,
        # A verifier can fail without itemized checks (e.g. it could not
        # evaluate at all) — honor each input's own verdict, never recompute
        # it from the check list alone.
        passed=all(result.passed for result in results) and not failed,
        failed_checks=failed,
        warnings=[w for result in results for w in result.warnings],
        severity=max_severity(check.severity for check in failed),
        blocks_release=any(check.blocks_release for check in failed),
        evidence=[item for result in results for item in result.evidence],
        metadata={
            "merged_from": [result.verifier_id for result in results],
            # Preserve per-verifier metadata instead of dropping it.
            "verifier_metadata": {result.verifier_id: result.metadata for result in results},
        },
    )


class Verifier(ABC):
    """Base class for deterministic verifiers.

    Subclasses set ``verifier_id`` (referenced by ``TaskSpec.verifier_ids``)
    and implement :meth:`verify`. Register new verifiers in
    ``verifiers/registry.py``.
    """

    verifier_id: ClassVar[str]

    @abstractmethod
    def verify(
        self,
        task: TaskSpec,
        trace: list[TraceEvent],
        final_state: dict[str, Any],
        run_id: str,
    ) -> VerifierResult:
        """Judge one finished run. Must be deterministic and side-effect free."""
        raise NotImplementedError
