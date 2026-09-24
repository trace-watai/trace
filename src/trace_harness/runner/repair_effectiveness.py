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

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

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


def build_repair_effectiveness(
    experiment_id: str, batches: list[Any], verdicts: Mapping[str, Any]
) -> RepairEffectivenessReport:
    """B1 for every control-on arm, artifact, control and model the experiment ran live.

    ``batches`` are :class:`~trace_harness.runner.verdict_agreement.RecordedBatch`
    items and ``verdicts`` maps a run id to its verifier result. Static replay
    never enters. The control-off side is the ``live_no_control`` runs of the
    same artifact, fork step and model; when there are none, that side has no
    completed runs and the formula returns null with its reason. Each arm gets
    its own entry, so a model that answers two arms is never pooled.
    """
    from trace_harness.runner.experiment import ConditionKind
    from trace_harness.runner.verdict_agreement import CONTROL_ON_KINDS, model_key

    on_groups: dict[tuple[str, str, str, int, str], list[tuple[Any, list[Any]]]] = {}
    off_groups: dict[tuple[str, int, str], list[tuple[Any, list[Any]]]] = {}
    for batch in batches:
        if batch.source_run_id is None:
            continue
        by_model: dict[str, list[Any]] = {}
        for entry in batch.summary.entries:
            by_model.setdefault(model_key(entry), []).append(entry)
        for model, entries in by_model.items():
            if batch.kind in CONTROL_ON_KINDS and batch.control:
                key = (batch.kind.value, batch.source_run_id, batch.control, batch.fork_step, model)
                on_groups.setdefault(key, []).append((batch, entries))
            elif batch.kind is ConditionKind.LIVE_NO_CONTROL and not batch.control:
                off_key = (batch.source_run_id, batch.fork_step, model)
                off_groups.setdefault(off_key, []).append((batch, entries))

    report_entries = []
    for (_, artifact, control, fork_step, model), on_runs in sorted(on_groups.items()):
        off_runs = off_groups.get((artifact, fork_step, model), [])
        control_on = _violations(on_runs, verdicts, fork_step, "live")
        control_off = _violations(off_runs, verdicts, fork_step, "live_no_control")
        value, reason = repair_effectiveness(control_on, control_off)
        report_entries.append(
            RepairEffectivenessEntry(
                artifact_id=artifact,
                control_id=control,
                fork_step=fork_step,
                model=model,
                control_on=control_on,
                control_off=control_off,
                repair_effectiveness=value,
                null_reason=reason,
            )
        )
    return RepairEffectivenessReport(experiment_id=experiment_id, entries=report_entries)


def _violations(
    runs: list[tuple[Any, list[Any]]], verdicts: Mapping[str, Any], fork_step: int, kind: str
) -> ConditionViolations:
    """Blocking failures after the fork over completed runs, across one side's batches."""
    from trace_harness.runner.verdict_agreement import blocking_after_fork, judged

    completed = [v for _, entries in runs for e in entries if (v := judged(e, verdicts))]
    names = sorted({batch.condition.name for batch, _ in runs})
    return ConditionViolations(
        condition="+".join(names) or kind,
        batch_id=runs[0][0].summary.batch_id if len(runs) == 1 else None,
        blocking_failures_after_fork=sum(1 for v in completed if blocking_after_fork(v, fork_step)),
        completed_runs=len(completed),
    )


def write_repair_effectiveness(experiment_dir: Path, report: RepairEffectivenessReport) -> Path:
    """Write the sidecar beside ``result.json``, atomically, as every artifact is written."""
    from trace_harness.tracing.artifact_store import _atomic_write_text

    path = experiment_dir / REPAIR_EFFECTIVENESS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, json.dumps(report.model_dump(mode="json"), indent=2) + "\n")
    return path
