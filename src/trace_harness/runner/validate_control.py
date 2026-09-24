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

    A static_ok artifact with no live condition recorded takes the short path,
    replay only. Every other artifact takes the live path, and so does a
    static_ok artifact whose live conditions ran.

    ``discard`` when any of these holds, on either path.

    - A positive sibling failed with the control installed in static replay.
      The #146 verdict is named, and it is ``rejected_overblocks`` whenever
      the pinned failure itself cleared. Siblings run whole from their own
      fixtures and never from the recording, so their result holds for any
      replay_mode.
    - The sibling pass rate, ``1 - sibling_failure_rate``, is below
      ``min_sibling_pass_rate``.

    On the short path, also when the #146 verdict is
    ``rejected_failure_persists`` or ``rejected_overblocks``, because a
    static_ok artifact is one whose static replay is trusted. On the live
    path, also when the share of completed control-on runs with a blocking
    failure after the fork is above ``max_live_violation_rate``, which is the
    failure persisting live.

    ``keep`` when nothing discards and every check of the path is met.

    - Short path. The #146 verdict is ``accepted`` and the sibling pass rate is
      at least ``min_sibling_pass_rate``.
    - Live path. A live control-on condition and a noise floor were recorded,
      ``verdict_agreement_rate`` is at least ``min_verdict_agreement_rate``,
      the sibling pass rate is at least ``min_sibling_pass_rate``, B1 from
      ``repair_effectiveness.json`` is at least ``min_repair_effectiveness``,
      and the live control condition beats the noise floor on
      ``post_block_outcomes`` by at least ``min_margin_over_noise_floor``.
      That margin is the share of blocked control-on runs labelled
      ``recovered`` minus the share of completed noise floor runs with no
      blocking failure after the fork.

    ``review`` otherwise, listing every unmet check. A missing sidecar, a
    missing entry or a null B1 is an unmet check, so it can never keep. A
    null metric is an unmet check too, since a missing number is never read
    as a pass.

On the live path a static verdict that rests on the recording, a
``rejected_failure_persists`` or a ``rejected_overblocks`` with every sibling
passing, is advisory and appears as a note. The recorded continuation cannot
react to the block, which is what ADR-0002 made static verdicts advisory for.

``keep`` never commits. It leaves the decision ``keep`` by ``policy`` in the
result, and a human commits the control with ``replay --apply-control
--commit``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from trace_harness.regression.repair_validation import ControlValidation, ControlVerdict
from trace_harness.regression.schemas import RegressionArtifact
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

STATIC_OK_SHORT_PATH = "static_ok_short_path"
LIVE_PATH = "live"
RulePath = Literal["static_ok_short_path", "live"]

_REJECTED = frozenset(
    {ControlVerdict.REJECTED_FAILURE_PERSISTS, ControlVerdict.REJECTED_OVERBLOCKS}
)


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
    static: StaticEvidence | None = None
    # The recorded live control-on condition and noise floor, by name.
    live_condition: str | None = None
    noise_floor_condition: str | None = None
    verdict_agreement_rate: float | None = None
    sibling_failure_rate: float | None = None
    post_block_outcomes: dict[str, int] | None = None
    effectiveness: RepairEffectivenessEntry | None = None
    # Why ``effectiveness`` is None: no sidecar, no entry, or several.
    effectiveness_note: str | None = None


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


def takes_short_path(replay_mode: str, live_condition: str | None) -> bool:
    """A static_ok artifact with no live control-on condition recorded."""
    return replay_mode == "static_ok" and live_condition is None


def decide_keep(evidence: KeepEvidence, rule: KeepRule) -> KeepOutcome:
    """Apply the rule in the module docstring. Pure, so every branch is testable alone."""
    short = takes_short_path(evidence.replay_mode, evidence.live_condition)
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


def _discard_reasons(evidence: KeepEvidence, rule: KeepRule, short: bool) -> list[str]:
    reasons: list[str] = []
    static = evidence.static
    if static is not None and static.failing_siblings:
        reasons.append(
            f"siblings fail, since positive sibling(s) {list(static.failing_siblings)} failed "
            f"with {evidence.control_id} installed in static replay (verdict "
            f"{static.verdict.value})"
        )
    passing = _sibling_pass_rate(evidence)
    if passing is not None and passing < rule.min_sibling_pass_rate:
        reasons.append(
            f"siblings fail, since the sibling pass rate {passing} is below the plan's minimum "
            f"{rule.min_sibling_pass_rate}"
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
    if entry is not None and rate is not None and rate > rule.max_live_violation_rate:
        on = entry.control_on
        reasons.append(
            f"the failure persists live, since {on.blocking_failures_after_fork} of "
            f"{on.completed_runs} completed run(s) under {on.condition} had a blocking failure "
            f"after the fork, above the plan's maximum share {rule.max_live_violation_rate}"
        )
    return reasons


def _short_path_checks(evidence: KeepEvidence, rule: KeepRule) -> list[RuleCheck]:
    static = evidence.static
    if static is None:
        verdict = RuleCheck(
            name="static_verdict",
            met=False,
            detail=(
                f"no replay-only condition recorded a per-control verdict for {evidence.control_id}"
            ),
        )
    else:
        accepted = static.verdict is ControlVerdict.ACCEPTED
        verdict = RuleCheck(
            name="static_verdict",
            value=static.verdict.value,
            met=accepted,
            detail=f"static replay verdict for {evidence.control_id} is {static.verdict.value}"
            + (f" ({static.reason})" if static.reason else ""),
        )
    return [verdict, _sibling_check(evidence, rule)]


def _live_path_checks(evidence: KeepEvidence, rule: KeepRule) -> list[RuleCheck]:
    return [
        _live_evidence_check(evidence),
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


def _live_evidence_check(evidence: KeepEvidence) -> RuleCheck:
    missing = []
    if evidence.live_condition is None:
        missing.append(f"no live condition with {evidence.control_id} installed was recorded")
    if evidence.noise_floor_condition is None:
        missing.append("no live_no_control condition was recorded, so there is no noise floor")
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
    met = value >= threshold
    return RuleCheck(
        name=name,
        value=value,
        threshold=threshold,
        met=met,
        detail=f"{name} {value} {'meets' if met else 'is below'} the plan's minimum {threshold}",
    )


def _sibling_pass_rate(evidence: KeepEvidence) -> float | None:
    rate = evidence.sibling_failure_rate
    return None if rate is None else round(1 - rate, 4)


def _sibling_check(evidence: KeepEvidence, rule: KeepRule) -> RuleCheck:
    return _minimum(
        "sibling_pass_rate",
        _sibling_pass_rate(evidence),
        rule.min_sibling_pass_rate,
        "sibling_failure_rate was not measured, so the sibling pass rate is unknown",
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
        "repair_effectiveness",
        round(entry.repair_effectiveness, 4),
        rule.min_repair_effectiveness,
        "",
    )


def _noise_floor_check(evidence: KeepEvidence, rule: KeepRule) -> RuleCheck:
    """Recovered share of blocked control-on runs minus the noise floor's clean share.

    ``post_block_outcomes`` counts every live control-on run, incomplete ones
    included as ``stalled``, so a control that leaves the agent stuck cannot
    beat the noise floor on runs that never finished. Runs the control never
    blocked say nothing about it and stay out of the denominator.
    """
    name, threshold = "margin_over_noise_floor", rule.min_margin_over_noise_floor

    def unmet(detail: str) -> RuleCheck:
        return RuleCheck(name=name, threshold=threshold, met=False, detail=detail)

    outcomes = evidence.post_block_outcomes or {}
    blocked = sum(n for label, n in outcomes.items() if label != "no_block_observed")
    if not blocked:
        return unmet("the control blocked no live run, so post_block_outcomes say nothing about it")
    entry = evidence.effectiveness
    off = entry.control_off if entry is not None else None
    if off is None or off.violation_rate is None:
        return unmet(
            "the noise floor has no completed runs in repair_effectiveness.json"
            if off is not None
            else "without a B1 entry there is no noise floor count to compare against"
        )
    recovered = outcomes.get("recovered", 0)
    clean = off.completed_runs - off.blocking_failures_after_fork
    margin = round(recovered / blocked - clean / off.completed_runs, 4)
    met = margin >= threshold
    return RuleCheck(
        name=name,
        value=margin,
        threshold=threshold,
        met=met,
        detail=(
            f"{recovered} of {blocked} blocked live run(s) recovered against {clean} of "
            f"{off.completed_runs} clean noise floor run(s) under {off.condition}, a margin of "
            f"{margin} that {'meets' if met else 'is below'} the plan's minimum {threshold}"
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
    notes = []
    if evidence.replay_mode == "static_ok":
        notes.append("a static_ok artifact took the live path because a live condition ran")
    if static is not None and static.verdict in _REJECTED and not static.failing_siblings:
        notes.append(
            f"static replay verdict {static.verdict.value} for {evidence.control_id} rests on "
            "the recorded continuation, which cannot react to the block, so it is advisory on "
            f"a {evidence.replay_mode} artifact ({static.reason})"
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
) -> tuple[RepairEffectivenessEntry | None, str | None]:
    """The one B1 entry for this artifact, control and pair of conditions, or why there is none.

    The artifact is matched by its test name, as the sidecar's model test names
    it, or by its source run id.
    """
    if report is None:
        return None, f"no {REPAIR_EFFECTIVENESS_FILE} beside the result, so B1 is unknown"
    if live_condition is None or noise_floor_condition is None:
        return (
            None,
            "no live condition and noise floor pair was recorded, so no B1 entry applies",
        )
    names = {artifact.test_name, artifact.source_run_id}
    matches = [
        e
        for e in report.entries
        if e.control_id == control_id
        and e.artifact_id in names
        and e.control_on.condition == live_condition
        and e.control_off.condition == noise_floor_condition
    ]
    if not matches:
        return None, (
            f"{REPAIR_EFFECTIVENESS_FILE} has no entry for {control_id} on "
            f"{artifact.test_name} comparing {live_condition} with {noise_floor_condition}"
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
