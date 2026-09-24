"""Keep, discard or review a control from live evidence (#203).

``trace-harness validate-control <control_id> --experiment <plan.json>``
runs the plan's conditions for one control through ``branch``, records them
with ``experiment record``, and applies the rule below to what came back. The
orchestration lives in ``cli.py`` beside ``branch`` and ``record``. This module
holds the rule, which is pure, and the readers that turn recorded artifacts
into its inputs.

The rule
    The thresholds come from the plan's ``keep_rule`` and nothing here has a
    default for any of them.

    The path is chosen once, by :func:`short_path_for`, before anything runs,
    and the command and the rule both read that answer. A static_ok artifact
    takes the short path, replay only, when its label was predicted with the
    control installed and the plan declares a replay-only condition for the
    control. Every other artifact takes the live path.

    ``discard`` when any of these holds, on either path.

    - A positive sibling failed with the control installed in static replay.
      The #146 verdict is named, and it is ``rejected_overblocks`` whenever
      the pinned failure itself cleared. Siblings run whole from their own
      fixtures and never from the recording, so their result holds for any
      replay_mode.
    - ``sibling_failure_rate`` is above zero. Siblings have zero tolerance,
      which is why the plan's ``min_sibling_pass_rate`` must be 1.0.

    On the short path, also when the #146 verdict is
    ``rejected_failure_persists`` or ``rejected_overblocks``, because a
    static_ok artifact is one whose static replay is trusted. On the live
    path, also when the share of completed control-on runs with a blocking
    failure after the fork is above ``max_live_violation_rate``, which is the
    failure persisting live.

    ``keep`` when nothing discards and every check of the path is met.

    - Both paths. The #146 verdict from a replay-only condition is
      ``accepted``, since ``replay --apply-control --commit``, the step that
      commits a kept control, commits nothing else. The sibling pass rate,
      ``1 - sibling_failure_rate``, is at least ``min_sibling_pass_rate``.
    - Live path, also. A live control-on condition and a noise floor were
      recorded and both are live evidence (see :func:`live_arm`),
      ``verdict_agreement_rate`` is at least ``min_verdict_agreement_rate``,
      B1 from ``repair_effectiveness.json`` is at least
      ``min_repair_effectiveness``, and the live control condition beats the
      noise floor by at least ``min_margin_over_noise_floor``. That margin is
      the share of blocked control-on runs labeled ``recovered`` in
      ``post_block_outcomes`` with no blocking failure after the fork, minus
      the share of completed noise floor runs with no blocking failure after
      the fork.

    ``review`` otherwise, listing every unmet check. A missing sidecar, a
    missing, stale or ambiguous entry and a null B1 are each an unmet check,
    so none of them can keep. A null metric is an unmet check too, since a
    missing number is never read as a pass.

On the live path a static rejection that rests on the recording, a
``rejected_failure_persists`` or a ``rejected_overblocks`` with every sibling
passing, does not discard. The recorded continuation cannot react to the
block, which is what ADR-0002 made static verdicts advisory for. It still
leaves ``static_verdict`` unmet, so the decision is review and a note says why.

A value is compared with its bound exactly, with room only for floating point
error, and is recorded in a check rounded to four places unless rounding would
carry it across the bound.

``keep`` never commits. It leaves the decision ``keep`` by ``policy`` in the
result, and a human commits the control with ``replay --apply-control
--commit``.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from trace_harness.regression.repair_validation import ControlValidation, ControlVerdict
from trace_harness.regression.schemas import RegressionArtifact
from trace_harness.runner.batch import BatchSummary
from trace_harness.runner.branch import CONTROL_VALIDATIONS_KEY
from trace_harness.runner.experiment import (
    ConditionKind,
    ConditionSpec,
    Decision,
    ExperimentSpec,
    KeepRule,
)
from trace_harness.runner.repair_effectiveness import (
    REPAIR_EFFECTIVENESS_FILE,
    RepairEffectivenessEntry,
    RepairEffectivenessReport,
)
from trace_harness.verifiers.base import VerifierResult

STATIC_OK_SHORT_PATH = "static_ok_short_path"
LIVE_PATH = "live"
RulePath = Literal["static_ok_short_path", "live"]
COMMIT_STEP = "replay --apply-control --commit"

_REJECTED = frozenset(
    {ControlVerdict.REJECTED_FAILURE_PERSISTS, ControlVerdict.REJECTED_OVERBLOCKS}
)
# Room for floating point error only, so 3/5 - 2/5 meets a minimum of 0.2 and
# 0.19996 does not.
_FLOAT_ERROR = 1e-9


@dataclass(frozen=True)
class StaticEvidence:
    """The #146 verdict one control earned in a replay-only condition."""

    verdict: ControlVerdict
    reason: str | None = None
    failing_siblings: tuple[str, ...] = ()
    siblings_run: int = 0

    @classmethod
    def from_validation(cls, validation: ControlValidation) -> StaticEvidence:
        reruns = validation.sibling_reruns
        return cls(
            verdict=validation.verdict,
            reason=validation.reason,
            failing_siblings=tuple(
                sorted(r.task_id or r.run_id for r in reruns if r.verdict == "FAIL")
            ),
            siblings_run=len(reruns),
        )


@dataclass(frozen=True)
class KeepEvidence:
    """Everything the rule reads, as plain values."""

    control_id: str
    replay_mode: str
    # Chosen by :func:`short_path_for` before anything ran, so the rule judges
    # the path the command ran.
    short_path: bool = False
    # Why a static_ok artifact took the live path, when it did.
    path_note: str | None = None
    static: StaticEvidence | None = None
    # The recorded live control-on condition and noise floor, by name, each
    # only when it is live evidence.
    live_condition: str | None = None
    noise_floor_condition: str | None = None
    # Why the plan's live condition or noise floor gave no live evidence,
    # from :func:`live_arm`.
    live_gap: str | None = None
    noise_floor_gap: str | None = None
    verdict_agreement_rate: float | None = None
    sibling_failure_rate: float | None = None
    post_block_outcomes: dict[str, int] | None = None
    # Runs labeled recovered in post_block_outcomes that still had a blocking
    # failure after the fork. None when nobody checked.
    recovered_with_blocking_failure: int | None = None
    effectiveness: RepairEffectivenessEntry | None = None
    # Why ``effectiveness`` is None: no sidecar, no entry, a stale one, or several.
    effectiveness_note: str | None = None

    def __post_init__(self) -> None:
        if self.short_path and self.replay_mode != "static_ok":
            raise ValueError(
                f"only a static_ok artifact takes the short path, got {self.replay_mode}"
            )
        recovered = (self.post_block_outcomes or {}).get("recovered", 0)
        failed = self.recovered_with_blocking_failure
        if failed is not None and not 0 <= failed <= recovered:
            raise ValueError(
                f"{failed} recovered run(s) with a blocking failure, out of {recovered} recovered"
            )


class RuleCheck(BaseModel):
    """One requirement of keep, with the number it read and the bound it met or missed."""

    model_config = ConfigDict(extra="forbid")

    name: str
    value: float | str | None = None
    threshold: float | None = None
    met: bool
    detail: str


class KeepOutcome(BaseModel):
    """What the rule decided, and every check it made on the way."""

    model_config = ConfigDict(extra="forbid")

    control_id: str
    replay_mode: str
    path: RulePath
    decision: Decision
    reasons: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    checks: list[RuleCheck] = Field(default_factory=list)


def decide_keep(evidence: KeepEvidence, rule: KeepRule) -> KeepOutcome:
    """Apply the rule in the module docstring. Pure, so every branch is testable alone."""
    short = evidence.short_path
    checks = _short_path_checks(evidence, rule) if short else _live_path_checks(evidence, rule)
    discards = _discard_reasons(evidence, rule, short)
    if discards:
        decision, reasons = Decision.DISCARD, discards
    elif all(check.met for check in checks):
        decision, reasons = Decision.KEEP, []
    else:
        decision, reasons = Decision.REVIEW, [c.detail for c in checks if not c.met]
    return KeepOutcome(
        control_id=evidence.control_id,
        replay_mode=evidence.replay_mode,
        path=STATIC_OK_SHORT_PATH if short else LIVE_PATH,
        decision=decision,
        reasons=reasons,
        notes=_notes(evidence, short),
        checks=checks,
    )


def _at_least(value: float, bound: float) -> bool:
    return value >= bound or math.isclose(value, bound, rel_tol=_FLOAT_ERROR, abs_tol=_FLOAT_ERROR)


def _shown(value: float, bound: float, met: bool) -> float:
    """``value`` rounded to four places, or exact when rounding would cross ``bound``."""
    rounded = round(value, 4)
    return rounded if _at_least(rounded, bound) == met else value


def _discard_reasons(evidence: KeepEvidence, rule: KeepRule, short: bool) -> list[str]:
    reasons: list[str] = []
    static = evidence.static
    if static is not None and static.failing_siblings:
        reasons.append(
            f"siblings fail, since positive sibling(s) {list(static.failing_siblings)} failed "
            f"with {evidence.control_id} installed in static replay (verdict "
            f"{static.verdict.value})"
        )
    sibling = _sibling_check(evidence, rule)
    if sibling.value is not None and not sibling.met:
        reasons.append(
            f"siblings fail, since the sibling pass rate {sibling.value} is below the plan's "
            f"minimum {rule.min_sibling_pass_rate}, and siblings have zero tolerance"
        )
    if short:
        if static is not None and static.verdict in _REJECTED and not static.failing_siblings:
            reasons.append(
                f"static replay verdict {static.verdict.value} ({static.reason}), which a "
                "static_ok artifact trusts"
            )
        return reasons
    entry = evidence.effectiveness
    rate = entry.control_on.violation_rate if entry is not None else None
    bound = rule.max_live_violation_rate
    if entry is not None and rate is not None and not _at_least(bound, rate):
        on = entry.control_on
        reasons.append(
            f"the failure persists live, since {on.blocking_failures_after_fork} of "
            f"{on.completed_runs} completed run(s) under {on.condition} had a blocking failure "
            f"after the fork, above the plan's maximum share {bound}"
        )
    return reasons


def _short_path_checks(evidence: KeepEvidence, rule: KeepRule) -> list[RuleCheck]:
    return [_static_verdict_check(evidence), _sibling_check(evidence, rule)]


def _live_path_checks(evidence: KeepEvidence, rule: KeepRule) -> list[RuleCheck]:
    return [
        _live_evidence_check(evidence),
        _static_verdict_check(evidence),
        _minimum(
            "verdict_agreement_rate",
            evidence.verdict_agreement_rate,
            rule.min_verdict_agreement_rate,
            "verdict_agreement_rate was not measured",
        ),
        _sibling_check(evidence, rule),
        _effectiveness_check(evidence, rule),
        _noise_floor_check(evidence, rule),
    ]


def _static_verdict_check(evidence: KeepEvidence) -> RuleCheck:
    """The #146 verdict must be accepted, since the commit step commits nothing else."""
    static = evidence.static
    if static is None:
        return RuleCheck(
            name="static_verdict",
            met=False,
            detail=(
                f"no replay-only condition recorded a per-control verdict for "
                f"{evidence.control_id}, and {COMMIT_STEP} commits only an accepted control"
            ),
        )
    accepted = static.verdict is ControlVerdict.ACCEPTED
    detail = f"static replay verdict for {evidence.control_id} is {static.verdict.value}" + (
        f" ({static.reason})" if static.reason else ""
    )
    if not accepted:
        detail += f", and {COMMIT_STEP} commits only an accepted control"
    return RuleCheck(name="static_verdict", value=static.verdict.value, met=accepted, detail=detail)


def _live_evidence_check(evidence: KeepEvidence) -> RuleCheck:
    missing = []
    if evidence.live_condition is None:
        missing.append(
            evidence.live_gap
            or f"no live condition with {evidence.control_id} installed was recorded"
        )
    if evidence.noise_floor_condition is None:
        missing.append(
            evidence.noise_floor_gap
            or "no live_no_control condition was recorded, so there is no noise floor"
        )
    detail = (
        f"a {evidence.replay_mode} artifact needs live evidence and static replay alone "
        f"cannot keep a control, yet {' and '.join(missing)}"
        if missing
        else f"live condition {evidence.live_condition} and noise floor "
        f"{evidence.noise_floor_condition} were recorded"
    )
    return RuleCheck(
        name="live_evidence",
        value="missing" if missing else "recorded",
        met=not missing,
        detail=detail,
    )


def _minimum(name: str, value: float | None, threshold: float, unmeasured: str) -> RuleCheck:
    if value is None:
        return RuleCheck(name=name, threshold=threshold, met=False, detail=unmeasured)
    met = _at_least(value, threshold)
    shown = _shown(value, threshold, met)
    return RuleCheck(
        name=name,
        value=shown,
        threshold=threshold,
        met=met,
        detail=f"{name} {shown} {'meets' if met else 'is below'} the plan's minimum {threshold}",
    )


def _sibling_check(evidence: KeepEvidence, rule: KeepRule) -> RuleCheck:
    """Zero tolerance, compared exactly, so no failure rate is small enough to round away."""
    name, threshold = "sibling_pass_rate", rule.min_sibling_pass_rate
    rate = evidence.sibling_failure_rate
    if rate is None:
        return RuleCheck(
            name=name,
            threshold=threshold,
            met=False,
            detail="sibling_failure_rate was not measured, so the sibling pass rate is unknown",
        )
    met = rate <= 1 - threshold
    shown = _shown(1 - rate, threshold, met)
    return RuleCheck(
        name=name,
        value=shown,
        threshold=threshold,
        met=met,
        detail=f"{name} {shown} {'meets' if met else 'is below'} the plan's minimum {threshold}",
    )


def _effectiveness_check(evidence: KeepEvidence, rule: KeepRule) -> RuleCheck:
    entry = evidence.effectiveness
    if entry is None:
        return RuleCheck(
            name="repair_effectiveness",
            threshold=rule.min_repair_effectiveness,
            met=False,
            detail=evidence.effectiveness_note or f"no B1 entry for {evidence.control_id}",
        )
    if entry.repair_effectiveness is None:
        return RuleCheck(
            name="repair_effectiveness",
            threshold=rule.min_repair_effectiveness,
            met=False,
            detail="repair_effectiveness is null because "
            + (entry.null_reason or "the sidecar recorded no reason"),
        )
    return _minimum(
        "repair_effectiveness", entry.repair_effectiveness, rule.min_repair_effectiveness, ""
    )


def _noise_floor_check(evidence: KeepEvidence, rule: KeepRule) -> RuleCheck:
    """Clean recovered share of blocked control-on runs minus the noise floor's clean share.

    Both shares count a run as clean only with no blocking failure after the
    fork, so a run labeled ``recovered`` that still failed a check the
    post-block classifier does not map, such as a missing escalation, counts
    against the control as the same run would on the noise floor.
    ``post_block_outcomes`` keeps incomplete runs as ``stalled``, so a control
    that leaves the agent stuck cannot beat the noise floor on runs that
    never finished. Runs the control never blocked say nothing about it and
    stay out of the denominator.
    """
    name, threshold = "margin_over_noise_floor", rule.min_margin_over_noise_floor

    def unmet(detail: str) -> RuleCheck:
        return RuleCheck(name=name, threshold=threshold, met=False, detail=detail)

    outcomes = evidence.post_block_outcomes or {}
    blocked = sum(n for label, n in outcomes.items() if label != "no_block_observed")
    if not blocked:
        return unmet("the control blocked no live run, so post_block_outcomes say nothing about it")
    recovered = outcomes.get("recovered", 0)
    failed = evidence.recovered_with_blocking_failure
    if recovered and failed is None:
        return unmet(
            "the live runs labeled recovered were not checked for a blocking failure after the fork"
        )
    entry = evidence.effectiveness
    off = entry.control_off if entry is not None else None
    if off is None or off.violation_rate is None:
        return unmet(
            "the noise floor has no completed runs in repair_effectiveness.json"
            if off is not None
            else "without a B1 entry there is no noise floor count to compare against"
        )
    clean_recovered = recovered - (failed or 0)
    clean = off.completed_runs - off.blocking_failures_after_fork
    margin = clean_recovered / blocked - clean / off.completed_runs
    met = _at_least(margin, threshold)
    shown = _shown(margin, threshold, met)
    return RuleCheck(
        name=name,
        value=shown,
        threshold=threshold,
        met=met,
        detail=(
            f"{clean_recovered} of {blocked} blocked live run(s) recovered with no blocking "
            f"failure after the fork, against {clean} of {off.completed_runs} clean noise floor "
            f"run(s) under {off.condition}, a margin of {shown} that "
            f"{'meets' if met else 'is below'} the plan's minimum {threshold}"
        ),
    )


def _notes(evidence: KeepEvidence, short: bool) -> list[str]:
    static = evidence.static
    if short:
        return [
            "the static_ok short path replays only, so verdict_agreement_rate, "
            "repair_effectiveness and the noise floor, which come from live runs, are not part "
            "of this decision"
        ]
    notes = [evidence.path_note] if evidence.path_note else []
    if static is not None and static.verdict in _REJECTED and not static.failing_siblings:
        notes.append(
            f"static replay verdict {static.verdict.value} for {evidence.control_id} rests on "
            "the recorded continuation, which cannot react to the block, so on a "
            f"{evidence.replay_mode} artifact it does not discard ({static.reason}); it still "
            f"leaves static_verdict unmet, since {COMMIT_STEP} commits only an accepted control"
        )
    return notes


# --- reading the recorded artifacts into the rule's inputs ---


@dataclass(frozen=True)
class ControlConditions:
    """The plan's conditions that answer for one control."""

    control_id: str
    static: tuple[ConditionSpec, ...]
    live: ConditionSpec | None
    swapped: tuple[ConditionSpec, ...]
    noise_floor: ConditionSpec | None

    def to_run(self, short: bool) -> list[ConditionSpec]:
        """Replay only on the short path, and every condition otherwise, in plan order."""
        if short:
            return list(self.static)
        chosen = [*self.static, *filter(None, [self.live]), *self.swapped]
        if self.noise_floor is not None:
            chosen.append(self.noise_floor)
        return chosen


def conditions_for_control(spec: ExperimentSpec, control_id: str) -> ControlConditions:
    """Select the conditions that install ``control_id`` alone, and the noise floor.

    A condition that installs the control beside others cannot say which one
    earned its result, which is the reason #146 validates one control at a
    time, so it is left out. The noise floor is the ``live_no_control``
    condition with nothing installed. More than one ``live`` condition for the
    control, or more than one noise floor, is refused, since the rule reads
    one of each.
    """
    own = [c for c in spec.conditions if c.control_ids == [control_id]]
    if not own:
        raise ValueError(
            f"{spec.experiment_id} declares no condition that installs {control_id} on its own"
        )
    live = [c for c in own if c.kind is ConditionKind.LIVE]
    noise = [
        c for c in spec.conditions if c.kind is ConditionKind.LIVE_NO_CONTROL and not c.control_ids
    ]
    for kind, found in (("live", live), ("live_no_control", noise)):
        if len(found) > 1:
            raise ValueError(
                f"{spec.experiment_id} declares {len(found)} {kind} conditions for {control_id} "
                f"({', '.join(c.name for c in found)}), and the keep rule reads one"
            )
    return ControlConditions(
        control_id=control_id,
        static=tuple(c for c in own if c.kind is ConditionKind.STATIC_REPLAY),
        live=live[0] if live else None,
        swapped=tuple(c for c in own if c.kind is ConditionKind.LIVE_SWAPPED),
        noise_floor=noise[0] if noise else None,
    )


def short_path_for(
    artifact: RegressionArtifact, control_id: str, conditions: ControlConditions
) -> tuple[bool, str | None]:
    """Whether validation takes the static_ok short path, and why a static_ok artifact did not.

    The label is trusted only for a control it was predicted with, which
    ``replay_mode_basis.control_ids`` names, and the short path needs a
    replay-only condition for the control to run.
    """
    if artifact.replay_mode != "static_ok":
        return False, None
    basis = artifact.replay_mode_basis
    predicted_with = list(basis.control_ids) if basis is not None else []
    if control_id not in predicted_with:
        return False, (
            f"a static_ok artifact took the live path, since its label was predicted with "
            f"{predicted_with} installed and says nothing about {control_id}"
        )
    if not conditions.static:
        return False, (
            "a static_ok artifact took the live path, since the plan declares no replay-only "
            f"condition for {control_id}"
        )
    return True, None


def live_arm(
    condition: ConditionSpec | None, batch: BatchSummary | None, skipped: str | None = None
) -> tuple[str | None, str | None]:
    """The condition's name when its batch is live evidence, else None and why it is not.

    A live agent, or a cassette of what one answered, is live evidence. A
    fixture arm is live evidence only when it plays a ``continuation_script``
    and at least one completed run's actions left the recording after the
    fork. With no script it plays the recorded actions after the fork, which
    is static replay under another name, and a script that repeats the
    recording is the same. Scripted arms stand in for a live agent in offline
    tests.
    """
    if condition is None:
        return None, None
    if skipped is not None:
        return None, f"{condition.name} was skipped ({skipped})"
    if batch is None:
        return None, f"{condition.name} was not recorded"
    if condition.agent_config.provider != "fixture":
        return condition.name, None
    if not condition.continuation_script:
        return None, (
            f"{condition.name} is a fixture arm with no continuation_script, so it replays the "
            "recording and is not live evidence"
        )
    left = [
        e
        for e in batch.entries
        if e.status == "completed" and e.first_post_fork_divergence_step is not None
    ]
    if not left:
        return None, (
            f"{condition.name} is a fixture arm whose continuation_script never left the "
            "recording in a completed run, so it replays the recording and is not live evidence"
        )
    return condition.name, None


def blocking_after_fork(verdict: VerifierResult, fork_step: int) -> bool:
    """A release-blocking failed check at a step after the fork, as B1 counts one."""
    return any(
        check.blocks_release and any(step > fork_step for step in check.step_ids)
        for check in verdict.failed_checks
    )


def recovered_with_blocking_failure(
    batch: BatchSummary, verdict_of: Callable[[str], VerifierResult | None], fork_step: int
) -> int:
    """Runs of a live batch labeled ``recovered`` that still failed a blocking check after the fork.

    ``verdict_of`` maps a run id to its :class:`VerifierResult`, or None when
    the run has none, which counts as a failure since nothing shows the run
    clean.
    """
    failed = 0
    for entry in batch.entries:
        if entry.post_block_outcome != "recovered":
            continue
        verdict = verdict_of(entry.run_id) if entry.run_id else None
        if verdict is None or blocking_after_fork(verdict, fork_step):
            failed += 1
    return failed


def static_evidence(batch_metadata: dict[str, Any], control_id: str) -> StaticEvidence | None:
    """The #146 verdict for ``control_id`` a replay-only branch batch recorded, if any."""
    for raw in batch_metadata.get(CONTROL_VALIDATIONS_KEY) or []:
        validation = ControlValidation.model_validate(raw)
        if validation.control_id == control_id:
            return StaticEvidence.from_validation(validation)
    return None


def read_repair_effectiveness(experiment_dir: Path) -> RepairEffectivenessReport | None:
    """The B1 sidecar #200 writes beside ``result.json``, or None when there is none."""
    path = experiment_dir / REPAIR_EFFECTIVENESS_FILE
    if not path.is_file():
        return None
    return RepairEffectivenessReport.model_validate_json(path.read_text(encoding="utf-8"))


def effectiveness_entry(
    report: RepairEffectivenessReport | None,
    artifact: RegressionArtifact,
    control_id: str,
    live_condition: str | None,
    noise_floor_condition: str | None,
    condition_batches: Mapping[str, str],
) -> tuple[RepairEffectivenessEntry | None, str | None]:
    """The one B1 entry for this artifact, control and pair of conditions, or why there is none.

    The artifact is matched by its source run id, as #200 writes it, or by its
    test name. The entry must also name the batches ``condition_batches``
    recorded for the two conditions, so a sidecar left from an earlier record
    of the same plan is never read as this one's.
    """
    if report is None:
        return None, f"no {REPAIR_EFFECTIVENESS_FILE} beside the result, so B1 is unknown"
    if live_condition is None or noise_floor_condition is None:
        return (
            None,
            "no live condition and noise floor pair that is live evidence was recorded, so no "
            "B1 entry applies",
        )
    names = {artifact.test_name, artifact.source_run_id}
    named = [
        e
        for e in report.entries
        if e.control_id == control_id
        and e.artifact_id in names
        and e.control_on.condition == live_condition
        and e.control_off.condition == noise_floor_condition
    ]
    if not named:
        return None, (
            f"{REPAIR_EFFECTIVENESS_FILE} has no entry for {control_id} on "
            f"{artifact.test_name} comparing {live_condition} with {noise_floor_condition}"
        )
    on_batch = condition_batches.get(live_condition)
    off_batch = condition_batches.get(noise_floor_condition)
    matches = [
        e for e in named if (e.control_on.batch_id, e.control_off.batch_id) == (on_batch, off_batch)
    ]
    if not matches:
        seen = sorted({f"{e.control_on.batch_id} and {e.control_off.batch_id}" for e in named})
        return None, (
            f"{REPAIR_EFFECTIVENESS_FILE} is stale, since its entry for {control_id} on "
            f"{artifact.test_name} compares batches {'; '.join(seen)}, and this result recorded "
            f"{on_batch} and {off_batch}"
        )
    if len(matches) > 1:
        return None, (
            f"{REPAIR_EFFECTIVENESS_FILE} has {len(matches)} entries for {control_id} on "
            f"{artifact.test_name}, and the rule reads one"
        )
    return matches[0], None


def render_keep_markdown(outcome: KeepOutcome, commit_command: str | None) -> str:
    """The rule's section of ``report.md``."""
    path = (
        "static_ok short path (replay only)"
        if outcome.path == STATIC_OK_SHORT_PATH
        else "live path"
    )
    lines = [
        "",
        "## Keep rule",
        "",
        f"`validate-control {outcome.control_id}` on a `{outcome.replay_mode}` artifact took "
        f"the {path}. Decision **{outcome.decision.value}** by policy.",
        "",
        "| check | value | threshold | met |",
        "|---|---|---|---|",
    ]
    for check in outcome.checks:
        value = "not measured" if check.value is None else check.value
        threshold = "" if check.threshold is None else check.threshold
        lines.append(f"| {check.name} | {value} | {threshold} | {'yes' if check.met else 'no'} |")
    for title, items in (("Reasons", outcome.reasons), ("Notes", outcome.notes)):
        if items:
            lines += ["", f"**{title}.**", "", *(f"- {item}" for item in items)]
    if commit_command:
        lines += [
            "",
            "Nothing was committed. A human commits the control with",
            "",
            f"    {commit_command}",
        ]
    return "\n".join(lines) + "\n"
