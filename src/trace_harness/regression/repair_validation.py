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
against, who produced that label (``predicted_by``), and whether the
artifact's own recorded basis supports it (``label_supported``). ADR-0002,
decision 2: "A static replay verdict on a control is advisory until the
artifact carries a measured replay-mode label." The same decision has the CI
collector gate control results only on ``static_ok``. Every ``static_ok``
label today is predicted by the materializer's fixed rule; #159 is what
measures one. This module follows the collector, so a verdict on a
``static_ok`` artifact whose basis supports the label is called gating, and
every place that reports it says the label is predicted until #159 measures
it. An accepted verdict on a ``live_required`` or ``unlabeled`` artifact, or
on a ``static_ok`` label its basis does not support, says the control held
under replay and nothing about a live agent.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from trace_harness.metrics.bounds import clopper_pearson_upper
from trace_harness.regression.schemas import (
    RegressionArtifact,
    ReplayMode,
    ReplayModePredictor,
    classify_replay_mode,
)

# 0.2.0: each control verdict records the artifact's replay_mode and whether
# that makes it gating or advisory; the rollup splits accepted verdicts the
# same way. A 0.1.0 file records no replay_mode, which reads as not recorded
# and advisory whatever its artifact says.
# 0.3.0: re-runs record their task fixture, the rollup reports over-blocking
# by task family with a 95% upper bound, and each verdict records whether the
# artifact's basis supports its label. A verdict without that record reads as
# unsupported and advisory.
REPAIR_VALIDATION_SCHEMA_VERSION = "0.3.0"

#: Task families are the directories directly under this one in fixtures/tasks.
TASK_FAMILY_ROOT = "refund_task_families"

VerdictStanding = Literal["gating", "advisory"]


def standing_for(
    replay_mode: ReplayMode | None,
    predicted_by: ReplayModePredictor | None,
    label_supported: bool,
) -> VerdictStanding:
    """Whether a verdict reached under this label can gate anything.

    Only ``static_ok`` gates, which is the rule the regression collector
    applies to control results (ADR-0002, decision 2). The label also has to
    come with a recorded basis, which ``predicted_by`` stands for, and that
    basis has to classify as the label (``label_supported``). This is
    ``gating_refusal`` applied to what a verdict recorded about its artifact.
    A label that was never recorded (None) is advisory.
    """
    gates = replay_mode == "static_ok" and predicted_by is not None and label_supported
    return "gating" if gates else "advisory"


def predictor_of(artifact: RegressionArtifact) -> ReplayModePredictor | None:
    """Who produced an artifact's label, or None when it carries no basis."""
    return artifact.replay_mode_basis.predicted_by if artifact.replay_mode_basis else None


def basis_supports_label(artifact: RegressionArtifact) -> bool:
    """Whether the artifact's recorded basis classifies as the label it carries.

    False when there is no basis. A label edited by hand after the
    materializer classified the basis is not supported by it.
    """
    basis = artifact.replay_mode_basis
    return basis is not None and classify_replay_mode(basis) == artifact.replay_mode


def gating_refusal(artifact: RegressionArtifact) -> str | None:
    """Why an artifact's label cannot back a gating verdict, or None when it can.

    The label has to be ``static_ok``, it has to come with its recorded basis,
    and that basis has to classify as ``static_ok`` under the same rule the
    materializer applied (``basis_supports_label``). A label edited by hand
    fails the last two.
    """
    if artifact.replay_mode != "static_ok":
        return f"the artifact is {artifact.replay_mode}"
    if artifact.replay_mode_basis is None:
        return "the artifact's static_ok label has no recorded basis"
    if not basis_supports_label(artifact):
        classified = classify_replay_mode(artifact.replay_mode_basis)
        return f"the artifact's recorded basis classifies as {classified}"
    return None


def describe_label(replay_mode: ReplayMode | None, predicted_by: ReplayModePredictor | None) -> str:
    """A replay label as every CLI surface prints it."""
    if replay_mode is None:
        return "replay_mode not recorded"
    if predicted_by is None:
        return f"replay_mode {replay_mode}"
    how = "measured" if predicted_by == "measured" else "predicted until #159 measures it"
    return f"replay_mode {replay_mode}, {how}"


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
    # The fixture the re-run was built from, as the artifact named it. Absent
    # before 0.3.0.
    task_fixture: str | None = None
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
    # The replay_mode of the artifact this verdict was reached against. None
    # means it was not recorded, as in every 0.1.0 file main writes today.
    # An unrecorded label is never compared with the artifact's current one.
    replay_mode: ReplayMode | None = None
    # Who produced that label, from the artifact's replay_mode_basis. None
    # when the artifact carried no basis or the label was not recorded.
    predicted_by: ReplayModePredictor | None = None
    # Whether the artifact's recorded basis classifies as its label
    # (``basis_supports_label``). False when not recorded, so a verdict
    # written without it stays advisory.
    label_supported: bool = False

    @model_validator(mode="before")
    @classmethod
    def drop_serialized_standing(cls, data: Any) -> Any:
        """Ignore a ``standing`` read back from a file and derive it again.

        ``standing`` is written out so a reader sees it without knowing the
        rule, but it always follows from ``replay_mode``, ``predicted_by``
        and ``label_supported``. Accepting it on input would let an edited
        ``standing`` promote an advisory verdict. Editing those fields is
        caught where gating is acted on: the control library and the metrics
        both check them against the retained artifact (``verdict_gates``).
        """
        if isinstance(data, dict) and "standing" in data:
            data = {k: v for k, v in data.items() if k != "standing"}
        return data

    @computed_field  # type: ignore[prop-decorator]
    @property
    def standing(self) -> VerdictStanding:
        return standing_for(self.replay_mode, self.predicted_by, self.label_supported)


def verdict_gates(verdict: ControlValidation, artifact: RegressionArtifact | None) -> bool:
    """Whether a verdict gates once checked against the artifact it names.

    The verdict's own label is only a claim. It gates when the verdict was
    recorded as gating and the retained artifact is present, carries the same
    label and predictor, and passes ``gating_refusal``. Anything else is
    advisory.
    """
    return (
        artifact is not None
        and verdict.standing == "gating"
        and verdict.replay_mode == artifact.replay_mode
        and verdict.predicted_by == predictor_of(artifact)
        and gating_refusal(artifact) is None
    )


def sibling_family(rerun: ReRun) -> str:
    """The task family a sibling re-run belongs to.

    A task under ``fixtures/tasks/refund_task_families/<family>/`` belongs to
    ``<family>``. Any other task is a family of one, keyed by task id so two
    spellings of one path count once. A re-run recorded before 0.3.0 has no
    fixture path and is keyed the same way.
    """
    if rerun.task_fixture:
        parts = PurePosixPath(rerun.task_fixture.replace("\\", "/")).parts
        if TASK_FAMILY_ROOT in parts:
            index = parts.index(TASK_FAMILY_ROOT)
            if len(parts) > index + 2:
                return f"{TASK_FAMILY_ROOT}/{parts[index + 1]}"
    return rerun.task_id or rerun.run_id


class OverBlockingSummary(BaseModel):
    """Positive siblings that failed with a control installed, counted by family.

    Siblings in one task family share a template and the mechanism under
    test, so a control that blocks one legitimate member tends to block its
    neighbors for the same reason. Each family is therefore one trial: it
    fails when any of its siblings failed, and ``upper_bound_95`` is the
    one-sided Clopper-Pearson 95% bound on the family failure rate. Counting
    siblings as trials would treat correlated re-runs as independent evidence
    and shrink the bound without anything new being learned.

    Families are counted over completed siblings only. An incomplete re-run
    shows neither a block nor its absence. ``siblings_run`` and
    ``siblings_failed`` keep their earlier meaning and count every re-run.
    """

    model_config = ConfigDict(extra="forbid")

    siblings_run: int = 0
    siblings_failed: int = 0
    independent_families: int = 0
    families_failed: int = 0
    # None when no family completed, since nothing was measured.
    upper_bound_95: float | None = None


def over_blocking_summary(controls: list[ControlValidation]) -> OverBlockingSummary:
    """Sibling and family counts over every control's sibling re-runs."""
    siblings = [s for c in controls for s in c.sibling_reruns]
    families: dict[str, bool] = {}
    for sibling in siblings:
        if sibling.verdict == "INCOMPLETE":
            continue
        family = sibling_family(sibling)
        families[family] = families.get(family, False) or sibling.verdict == "FAIL"
    failed = sum(families.values())
    bound = clopper_pearson_upper(failed, len(families))
    return OverBlockingSummary(
        siblings_run=len(siblings),
        siblings_failed=sum(1 for s in siblings if s.verdict == "FAIL"),
        independent_families=len(families),
        families_failed=failed,
        upper_bound_95=None if bound is None else round(bound, 4),
    )


class ValidationRollup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accepted: int = 0
    rejected: int = 0
    skipped: int = 0
    # ``accepted`` split by standing. The two always sum to ``accepted``.
    accepted_gating: int = 0
    accepted_advisory: int = 0
    over_blocking: OverBlockingSummary = Field(default_factory=OverBlockingSummary)


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
            over_blocking=over_blocking_summary(self.controls),
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


def skipped_control(
    name: str,
    *,
    replay_mode: ReplayMode | None = None,
    predicted_by: ReplayModePredictor | None = None,
    label_supported: bool = False,
) -> ControlValidation:
    """A prescribed control with no registered guardrail, reported honestly."""
    return ControlValidation(
        control=name,
        verdict=ControlVerdict.SKIPPED,
        reason="not_materializable: no registered guardrail for this control yet",
        replay_mode=replay_mode,
        predicted_by=predicted_by,
        label_supported=label_supported,
    )
