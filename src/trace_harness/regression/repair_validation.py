"""Per-control validation verdicts as a durable artifact (issue #146).

``replay --apply-control`` already answers "does the control kill the pinned
failure without breaking the siblings?", but it installs every control at once
and the answer survives only as printed output and an exit code. This module
makes the answer a value: one verdict per prescribed control, with the re-runs
that produced it, written as ``repair_validation.json``.

The verdict logic here is pure so it can be tested without running fixtures.
The orchestration that actually replays scenarios lives in ``cli.py``, which is
where the fixture runner and verifier are already wired together.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

REPAIR_VALIDATION_SCHEMA_VERSION = "0.1.0"


class ControlVerdict(StrEnum):
    """What a single control earned when validated in isolation.

    ``skipped`` is not a failure. A repair package prescribes controls by name
    and most of them have no registered guardrail yet, so reporting them as
    skipped is how the artifact stays honest instead of implying coverage that
    does not exist.
    """

    ACCEPTED = "accepted"
    REJECTED_FAILURE_PERSISTS = "rejected_failure_persists"
    REJECTED_OVERBLOCKS = "rejected_overblocks"
    SKIPPED = "skipped"


class ReRun(BaseModel):
    """One replay performed while validating a control."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    task_id: str | None = None
    verdict: str  # "PASS" or "FAIL", as the verifier reported it
    # Pinned checks that stopped firing once this control was installed. Only
    # meaningful on the originating re-run.
    cleared_checks: list[str] = Field(default_factory=list)
    failed_checks: list[str] = Field(default_factory=list)


class ControlValidation(BaseModel):
    """The verdict for one prescribed control, with its evidence."""

    model_config = ConfigDict(extra="forbid")

    control: str  # RepairControl.name as the repair package prescribed it
    verdict: ControlVerdict
    # Why a control was skipped or rejected. None when accepted.
    reason: str | None = None
    guardrail_ref: str | None = None
    control_id: str | None = None
    originating_rerun: ReRun | None = None
    sibling_reruns: list[ReRun] = Field(default_factory=list)


class ValidationRollup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accepted: int = 0
    rejected: int = 0
    skipped: int = 0


class RepairValidation(BaseModel):
    """Per-control verdicts for one regression artifact's repair package."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = REPAIR_VALIDATION_SCHEMA_VERSION
    run_id: str  # the artifact's source_run_id, so this links back to the failure
    test_name: str
    # Batch grouping every re-run this validation performed, so
    # ``list-runs --batch`` can show the whole session.
    batch_id: str | None = None
    # Where the control names came from, since a replay may run without the
    # originating run directory present.
    controls_source: str = "repair_package"
    controls: list[ControlValidation] = Field(default_factory=list)
    rollup: ValidationRollup = Field(default_factory=ValidationRollup)

    def rebuild_rollup(self) -> RepairValidation:
        """Recount the rollup from ``controls`` so the two can never disagree."""
        rejected = {
            ControlVerdict.REJECTED_FAILURE_PERSISTS,
            ControlVerdict.REJECTED_OVERBLOCKS,
        }
        self.rollup = ValidationRollup(
            accepted=sum(1 for c in self.controls if c.verdict is ControlVerdict.ACCEPTED),
            rejected=sum(1 for c in self.controls if c.verdict in rejected),
            skipped=sum(1 for c in self.controls if c.verdict is ControlVerdict.SKIPPED),
        )
        return self

    @property
    def has_rejection(self) -> bool:
        """True when any control was rejected, which is what ``--fail-on-rejected`` gates on."""
        return self.rollup.rejected > 0


def decide_verdict(
    *,
    expected_checks: set[str],
    pinned_failed_checks: set[str],
    pinned_introduced_blocking: set[str],
    failing_siblings: list[str],
) -> tuple[ControlVerdict, str | None]:
    """Decide one control's verdict from what its re-runs produced.

    ``rejected_failure_persists`` wins over ``rejected_overblocks`` when both
    apply, because a control that does not fix the failure it was prescribed
    for is rejected on its own terms and the sibling result adds nothing.

    A blocking check that fires on the pinned scenario but was never pinned
    counts as overblocking. The control caused a failure that was not there
    before, which is the same harm as breaking a sibling.
    """
    still_firing = sorted(expected_checks & pinned_failed_checks)
    if still_firing:
        return (
            ControlVerdict.REJECTED_FAILURE_PERSISTS,
            f"pinned check(s) {still_firing} still fired with the control installed",
        )
    if pinned_introduced_blocking:
        return (
            ControlVerdict.REJECTED_OVERBLOCKS,
            f"control introduced blocking check(s) {sorted(pinned_introduced_blocking)} "
            "that this artifact never pinned",
        )
    if failing_siblings:
        return (
            ControlVerdict.REJECTED_OVERBLOCKS,
            f"positive sibling(s) {sorted(failing_siblings)} failed with the control installed",
        )
    return ControlVerdict.ACCEPTED, None


def skipped_control(name: str) -> ControlValidation:
    """A prescribed control with no registered guardrail, reported honestly."""
    return ControlValidation(
        control=name,
        verdict=ControlVerdict.SKIPPED,
        reason="not_materializable: no registered guardrail for this control yet",
    )
