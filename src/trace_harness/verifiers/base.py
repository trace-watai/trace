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
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel, Field, model_validator

from trace_harness.tasks.schemas import Severity, TaskSpec, max_severity
from trace_harness.tracing.events import TraceEvent

VERIFIER_RESULT_SCHEMA_VERSION = (
    "0.4.0"  # 0.4.0: verdict (pass/fail/incomplete); 0.3.0: EvidenceKind gained ESCALATION_RECORD
)
VERIFIER_INPUT_SCHEMA_VERSION = "0.1.0"


class VerifierInput(BaseModel):
    """Everything a verifier needs to judge one run.

    Bundles the inputs into a single serializable object so callers,
    storage (Samrath), and regression replay (Samir) can round-trip a
    complete verification request through JSON without juggling loose
    parameters.

    ``initial_state`` is optional because some verification contexts
    (re-running from artifacts) may only have the final snapshot.  When
    present it lets checks compare before/after (future use).
    """

    schema_version: str = VERIFIER_INPUT_SCHEMA_VERSION
    task: TaskSpec
    trace: list[TraceEvent]
    initial_state: dict[str, Any] = Field(default_factory=dict)
    final_state: dict[str, Any]
    run_id: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_parts(
        cls,
        *,
        task: TaskSpec,
        trace: list[TraceEvent],
        final_state: dict[str, Any],
        run_id: str,
        initial_state: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> VerifierInput:
        """Convenience factory mirroring the old verify() signature."""
        return cls(
            task=task,
            trace=trace,
            initial_state=initial_state or {},
            final_state=final_state,
            run_id=run_id,
            metadata=metadata or {},
        )


class EvidenceKind(StrEnum):
    """Closed vocabulary of evidence tags shared with the dashboard.

    Constraining ``EvidenceItem.kind`` to this enum keeps producers and the
    frontend renderer aligned on one set of values. Add a member here (and the
    matching case in the dashboard) when a verifier needs to emit a new kind —
    never an ad-hoc string.
    """

    ORDER_RECORD = "order_record"
    REFUND_RECORD = "refund_record"
    TICKET_RECORD = "ticket_record"
    ESCALATION_RECORD = "escalation_record"
    POLICY_RULES = "policy_rules"
    RETRIEVAL_PROVENANCE = "retrieval_provenance"
    PROVENANCE_QUOTE = "provenance_quote"
    FINAL_ANSWER = "final_answer"


class EvidenceItem(BaseModel):
    """One piece of evidence supporting a check outcome.

    ``kind`` is a machine-usable tag from :class:`EvidenceKind`; ``data``
    carries the raw facts so the dashboard can render them without re-deriving
    anything.
    """

    kind: EvidenceKind
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


class VerifierVerdict(StrEnum):
    """Three states, not two. ``incomplete`` is a run that never reached a
    final answer: it recorded no violations, which is not the same as passing.
    Consumers that count passes must count ``verdict``, not ``passed``."""

    PASS = "pass"
    FAIL = "fail"
    INCOMPLETE = "incomplete"


class VerifierResult(BaseModel):
    """The verdict for one run from one verifier (or a merge of several)."""

    schema_version: str = VERIFIER_RESULT_SCHEMA_VERSION
    verifier_id: str
    run_id: str
    passed: bool
    # pass / fail / incomplete. Derived from ``passed`` when absent (pre-0.4.0
    # files); set to ``incomplete`` by :func:`mark_incomplete` when the run
    # never completed, in which case ``passed`` is forced to False.
    verdict: VerifierVerdict | None = None
    failed_checks: list[FailedCheck] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    # Highest severity among failed checks; None when passed.
    severity: Severity | None = None
    blocks_release: bool = False
    # Run-level evidence not tied to a single check (e.g. retrieval provenance).
    evidence: list[EvidenceItem] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _derive_verdict(self) -> VerifierResult:
        if self.verdict is None:
            self.verdict = VerifierVerdict.PASS if self.passed else VerifierVerdict.FAIL
        elif self.verdict is VerifierVerdict.INCOMPLETE and self.passed:
            raise ValueError("an incomplete verdict cannot have passed=True")
        return self

    @property
    def has_violations(self) -> bool:
        """True when the checks recorded a violation, whatever the verdict.

        Attribution and failure bundles follow the evidence, not the verdict.
        A run that broke a rule and *then* died is ``incomplete`` — it cannot
        be counted as a failure in a pass rate — but it still has something to
        explain, and ``mark_incomplete`` deliberately keeps its
        ``failed_checks``, ``severity`` and ``blocks_release``. Gating the
        bundle on the verdict would assert a release-blocking violation and
        then refuse to say what it was.
        """
        return bool(self.failed_checks)


def mark_incomplete(
    result: VerifierResult, *, status: str, termination_reason: str
) -> VerifierResult:
    """Return ``result`` re-labelled as ``incomplete`` because the run never completed.

    Keeps ``failed_checks``, ``severity`` and ``blocks_release`` exactly as the
    checks computed them (a violation before the run died is still a
    violation); forces ``passed`` to False so nothing counting ``passed``
    treats a dead run as a pass; and records why in ``warnings`` so the
    artifact carries the reason, not just the CLI output.
    """
    note = (
        f"run did not complete (status={status}, termination={termination_reason}); "
        "verdict is incomplete: no violations were recorded, which is not a pass"
    )
    return result.model_copy(
        update={
            "verdict": VerifierVerdict.INCOMPLETE,
            "passed": False,
            "warnings": [*result.warnings, note],
        }
    )


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
        input: VerifierInput,
    ) -> VerifierResult:
        """Judge one finished run. Must be deterministic and side-effect free."""
        raise NotImplementedError
