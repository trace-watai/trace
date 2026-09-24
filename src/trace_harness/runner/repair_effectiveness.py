"""Repair effectiveness, the B1 number in ``docs/methodology_metrics.md`` (#200).

B1 asks how much a control reduces blocking failures for a live agent that
continues from the step where the failure happened. It is computed per
starting point and control from the live conditions only, and never from
static replay. It lives in ``repair_effectiveness.json`` beside an
experiment's ``result.json`` and stays out of :class:`ExperimentMetrics`,
whose eight fields are fixed by the #27 memo.

``#200`` writes this file from the branch batches, and ``#203`` reads it when
deciding whether to keep a control.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

REPAIR_EFFECTIVENESS_SCHEMA_VERSION = "0.1.0"
REPAIR_EFFECTIVENESS_FILE = "repair_effectiveness.json"


class ConditionViolations(BaseModel):
    """Blocking failures after the fork over completed runs, for one condition.

    A blocking failure counts only checks whose ``step_ids`` fall after the
    fork step. Runs that ended incomplete are left out of both counts.
    """

    model_config = ConfigDict(extra="forbid")

    condition: str
    batch_id: str | None = None
    blocking_failures_after_fork: int = Field(ge=0)
    completed_runs: int = Field(ge=0)

    @property
    def violation_rate(self) -> float | None:
        if self.completed_runs == 0:
            return None
        return self.blocking_failures_after_fork / self.completed_runs


class RepairEffectivenessEntry(BaseModel):
    """B1 for one starting point, one control and one live model."""

    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    control_id: str
    fork_step: int
    model: str | None = None
    control_on: ConditionViolations
    control_off: ConditionViolations
    repair_effectiveness: float | None = None
    null_reason: str | None = None


class RepairEffectivenessReport(BaseModel):
    """The ``repair_effectiveness.json`` sidecar of one experiment."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = REPAIR_EFFECTIVENESS_SCHEMA_VERSION
    experiment_id: str
    entries: list[RepairEffectivenessEntry] = Field(default_factory=list)


def repair_effectiveness(
    control_on: ConditionViolations, control_off: ConditionViolations
) -> tuple[float | None, str | None]:
    """``1 - violation_rate(on) / violation_rate(off)``, or null with a reason.

    The control-off condition is the baseline. Without completed runs on both
    sides, or with a baseline that never violated, the ratio has no meaning,
    and a number would read as a measurement nobody took.
    """
    on, off = control_on.violation_rate, control_off.violation_rate
    if on is None:
        return None, f"no completed runs under {control_on.condition}"
    if off is None:
        return None, f"no completed runs under {control_off.condition}"
    if off == 0:
        return None, f"the baseline {control_off.condition} recorded no blocking failure"
    return 1 - on / off, None
