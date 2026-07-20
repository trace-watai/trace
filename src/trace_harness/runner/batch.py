"""Batch execution: run a suite (tasks × agent configs) with failure isolation.

The single-run path already isolates failures *inside* a run (a crashed run
still writes ``run_result.json``). The batch adds the missing layer: a failure
*around* a run — a malformed fixture, a missing script, an environment that
won't build — is caught, recorded as a ``setup_error`` entry, and the batch
keeps going. One bad task never crashes the batch.

Outputs:
    - each run still lands in ``runs/{run_id}/`` (unchanged single-run layout)
    - one batch summary at ``runs/batches/{batch_id}/batch_summary.json``,
      referencing those run ids, with per-run metadata and aggregates for the
      dashboard.

Cost is intentionally out of scope for now (fixture runs have none; real-cost
capture is a follow-up). Latency is recorded from run timestamps.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from trace_harness.runner.pipeline import PipelineResult, run_task_pipeline
from trace_harness.runner.result import RunStatus
from trace_harness.runner.suite import AgentConfig, SuiteSpec
from trace_harness.tracing.artifact_store import ArtifactStore
from trace_harness.tracing.events import utc_now

logger = logging.getLogger(__name__)

BATCH_SUMMARY_SCHEMA_VERSION = "0.1.0"
BATCH_SUMMARY = "batch_summary.json"

# Entry statuses that mean "did not produce a usable, completed run".
_ERROR_STATUSES = ("error", "setup_error")


class BatchRunEntry(BaseModel):
    """One cell of the suite: one task under one agent config."""

    run_id: str | None  # None when setup failed before a run existed
    task_id: str
    task_path: str
    agent_label: str
    provider: str
    model: str | None = None
    prompt_version: str | None = None
    status: str  # completed / terminated / error / setup_error
    termination_reason: str | None = None
    steps_taken: int | None = None
    verifier_passed: bool | None = None  # None if verify didn't run
    verifier_id: str | None = None
    severity: str | None = None
    latency_ms: float | None = None
    error: str | None = None


class BatchAggregates(BaseModel):
    """Roll-up metrics over the batch (pass_rate is over *completed* runs only)."""

    total: int
    completed: int
    errored: int
    verifier_passed: int
    verifier_failed: int
    pass_rate: float | None = None  # None when no completed run had a verdict
    by_agent: dict[str, dict[str, int]] = Field(default_factory=dict)


class BatchSummary(BaseModel):
    """The dashboard-consumable result of one batch run."""

    schema_version: str = BATCH_SUMMARY_SCHEMA_VERSION
    batch_id: str
    suite_id: str
    started_at: datetime
    finished_at: datetime
    agent_configs: list[AgentConfig]
    entries: list[BatchRunEntry]
    aggregates: BatchAggregates


def new_batch_id() -> str:
    """Sortable, collision-resistant batch id: batch_<utc timestamp>_<hex8>."""
    return f"batch_{utc_now():%Y%m%dT%H%M%SZ}_{uuid.uuid4().hex[:8]}"


def batch_dir(runs_dir: Path, batch_id: str) -> Path:
    return runs_dir / "batches" / batch_id


def summary_path(runs_dir: Path, batch_id: str) -> Path:
    return batch_dir(runs_dir, batch_id) / BATCH_SUMMARY


class BatchRunner:
    """Runs a suite over one artifact store, isolating per-cell failures."""

    def __init__(self, store: ArtifactStore):
        self.store = store

    def run(self, suite: SuiteSpec) -> BatchSummary:
        started_at = utc_now()
        entries: list[BatchRunEntry] = []
        for config in suite.agent_configs:
            for task_path in suite.tasks:
                entries.append(self._run_cell(config, task_path))
        finished_at = utc_now()

        summary = BatchSummary(
            batch_id=new_batch_id(),
            suite_id=suite.suite_id,
            started_at=started_at,
            finished_at=finished_at,
            agent_configs=suite.agent_configs,
            entries=entries,
            aggregates=_aggregate(entries),
        )
        self._write_summary(summary)
        return summary

    def _run_cell(self, config: AgentConfig, task_path: str) -> BatchRunEntry:
        try:
            result = run_task_pipeline(task_path, config, self.store)
            return _entry_from_pipeline(result, config, task_path)
        except Exception as exc:  # noqa: BLE001 — isolate the cell; the batch goes on
            logger.warning(
                "batch cell failed (agent=%s, task=%s): %s", config.label, task_path, exc
            )
            return _setup_error_entry(config, task_path, exc)

    def _write_summary(self, summary: BatchSummary) -> Path:
        path = summary_path(self.store.runs_dir, summary.batch_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(summary.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8"
        )
        return path


def _entry_from_pipeline(
    result: PipelineResult, config: AgentConfig, task_path: str
) -> BatchRunEntry:
    run = result.run_result
    verifier = result.verifier_result
    latency_ms = round((run.finished_at - run.started_at).total_seconds() * 1000, 1)
    return BatchRunEntry(
        run_id=run.run_id,
        task_id=run.task_id,
        task_path=str(task_path),
        agent_label=config.label,
        provider=result.run_config.provider,
        model=result.run_config.model,
        prompt_version=result.run_config.prompt_version,
        status=str(run.status),
        termination_reason=str(run.termination_reason),
        steps_taken=run.steps_taken,
        verifier_passed=(verifier.passed if verifier is not None else None),
        verifier_id=(verifier.verifier_id if verifier is not None else None),
        severity=(verifier.severity.value if verifier and verifier.severity else None),
        latency_ms=latency_ms,
        error=run.error,
    )


def _setup_error_entry(config: AgentConfig, task_path: str, exc: Exception) -> BatchRunEntry:
    return BatchRunEntry(
        run_id=None,
        task_id=Path(task_path).stem,  # real task_id unknown if load failed
        task_path=str(task_path),
        agent_label=config.label,
        provider=config.provider,
        model=config.model,
        prompt_version=config.prompt_version,
        status="setup_error",
        error=f"{type(exc).__name__}: {exc}",
    )


def _aggregate(entries: list[BatchRunEntry]) -> BatchAggregates:
    completed = [e for e in entries if e.status == str(RunStatus.COMPLETED)]
    # Pass/fail counts consider only completed runs: an incomplete run that
    # recorded no violations is not a genuine pass (mirrors the CLI's CI gate).
    passed = sum(1 for e in completed if e.verifier_passed is True)
    failed = sum(1 for e in completed if e.verifier_passed is False)
    errored = sum(1 for e in entries if e.status in _ERROR_STATUSES)
    verdicts = passed + failed
    pass_rate = round(passed / verdicts, 4) if verdicts else None

    by_agent: dict[str, dict[str, int]] = {}
    for e in entries:
        bucket = by_agent.setdefault(e.agent_label, {"passed": 0, "failed": 0, "errored": 0})
        if e.status == str(RunStatus.COMPLETED) and e.verifier_passed is True:
            bucket["passed"] += 1
        elif e.status == str(RunStatus.COMPLETED) and e.verifier_passed is False:
            bucket["failed"] += 1
        if e.status in _ERROR_STATUSES:
            bucket["errored"] += 1

    return BatchAggregates(
        total=len(entries),
        completed=len(completed),
        errored=errored,
        verifier_passed=passed,
        verifier_failed=failed,
        pass_rate=pass_rate,
        by_agent=by_agent,
    )
