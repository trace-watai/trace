"""Per-control validation verdicts as a durable artifact (issue #146).

``replay --apply-control`` already answers "does the control kill the pinned
failure without breaking the siblings?", but it installs every control at once
and the answer survives only as printed output and an exit code. This module
makes the answer a value: one verdict per prescribed control, with the re-runs
that produced it, written as ``repair_validation.json``.

The verdict logic here is pure so it can be tested without running fixtures.
The orchestration that actually replays scenarios lives in ``cli.py``, which is
where the fixture runner and verifier are already wired together.

Each verdict also records the ``replay_mode`` of the artifact it was reached
against. ADR-0002 makes a static replay verdict advisory until the artifact is
labeled ``static_ok``, so an accepted verdict on a ``live_required`` or
``unlabeled`` artifact says the control held under replay and nothing about a
live agent. The collector already reads the label that way; recording it here
lets the control library and the metrics read it the same way.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from trace_harness.regression.schemas import ReplayMode

# 0.2.0: each control verdict records the artifact's replay_mode and whether
# that makes it gating or advisory; the rollup splits accepted verdicts the
# same way. Files written at 0.1.0 read as unlabeled, which is advisory.
REPAIR_VALIDATION_SCHEMA_VERSION = "0.2.0"

VerdictStanding = Literal["gating", "advisory"]


def standing_for(replay_mode: ReplayMode) -> VerdictStanding:
    """Whether a verdict reached under ``replay_mode`` can gate anything.

    Only ``static_ok`` gates, which is the rule the regression collector
    already applies to control results (ADR-0002, decision 2).
    """
    return "gating" if replay_mode == "static_ok" else "advisory"


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
    verdict: Literal["PASS", "FAIL", "INCOMPLETE"]
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
    # The replay_mode of the artifact this verdict was reached against. Files
    # written before 0.2.0 did not record it and read as unlabeled.
    replay_mode: ReplayMode = "unlabeled"

    @model_validator(mode="before")
    @classmethod
    def drop_serialized_standing(cls, data: Any) -> Any:
        """Ignore a ``standing`` read back from a file and derive it again.

        ``standing`` is written out so a reader sees it without knowing the
        rule, but it always follows from ``replay_mode``. Accepting it on input
        would let an edited file promote an advisory verdict to gating.
        """
        if isinstance(data, dict) and "standing" in data:
            data = {k: v for k, v in data.items() if k != "standing"}
        return data

    @computed_field  # type: ignore[prop-decorator]
    @property
    def standing(self) -> VerdictStanding:
        return standing_for(self.replay_mode)


class ValidationRollup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accepted: int = 0
    rejected: int = 0
    skipped: int = 0
    # ``accepted`` split by standing. The two always sum to ``accepted``.
    accepted_gating: int = 0
    accepted_advisory: int = 0


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

    @model_validator(mode="after")
    def rebuild_rollup(self) -> RepairValidation:
        """Recount the rollup from ``controls`` so the two can never disagree."""
        rejected = {
            ControlVerdict.REJECTED_FAILURE_PERSISTS,
            ControlVerdict.REJECTED_OVERBLOCKS,
        }
        accepted = [c for c in self.controls if c.verdict is ControlVerdict.ACCEPTED]
        self.rollup = ValidationRollup(
            accepted=len(accepted),
            rejected=sum(1 for c in self.controls if c.verdict in rejected),
            skipped=sum(1 for c in self.controls if c.verdict is ControlVerdict.SKIPPED),
            accepted_gating=sum(1 for c in accepted if c.standing == "gating"),
            accepted_advisory=sum(1 for c in accepted if c.standing == "advisory"),
        )
        return self

    @property
    def has_rejection(self) -> bool:
        """True when any control was rejected, which is what ``--fail-on-rejected`` gates on."""
        return any(
            c.verdict
            in {ControlVerdict.REJECTED_FAILURE_PERSISTS, ControlVerdict.REJECTED_OVERBLOCKS}
            for c in self.controls
        )

    @property
    def has_incomplete(self) -> bool:
        """An interrupted validation cannot establish a successful replay gate."""
        return any(
            rerun.verdict == "INCOMPLETE"
            for control in self.controls
            for rerun in [control.originating_rerun, *control.sibling_reruns]
            if rerun is not None
        )


def decide_verdict(
    *,
    expected_checks: set[str],
    pinned_failed_checks: set[str],
    pinned_introduced_blocking: set[str],
    failing_siblings: list[str],
    pinned_completed: bool = True,
    incomplete_siblings: list[str] | None = None,
) -> tuple[ControlVerdict, str | None]:
    """Decide one control's verdict from what its re-runs produced.

    ``rejected_failure_persists`` wins over ``rejected_overblocks`` when both
    apply, because a control that does not fix the failure it was prescribed
    for is rejected on its own terms and the sibling result adds nothing.

    A blocking check that fires on the pinned scenario but was never pinned
    counts as overblocking. The control caused a failure that was not there
    before, which is the same harm as breaking a sibling.
    """
    if not pinned_completed or incomplete_siblings:
        detail = (
            "the pinned replay did not complete"
            if not pinned_completed
            else f"positive sibling(s) {sorted(incomplete_siblings)} did not complete"
        )
        return ControlVerdict.SKIPPED, f"validation_incomplete: {detail}"
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


def skipped_control(name: str, *, replay_mode: ReplayMode = "unlabeled") -> ControlValidation:
    """A prescribed control with no registered guardrail, reported honestly."""
    return ControlValidation(
        control=name,
        verdict=ControlVerdict.SKIPPED,
        reason="not_materializable: no registered guardrail for this control yet",
        replay_mode=replay_mode,
    )
