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

Fixture and cassette-replay runs call no provider, so their cost is recorded as
exactly zero. A live run is priced from the usage its own trace recorded. A
provider or model with no price records ``null``, and aggregate coverage makes
that missing telemetry visible.

A live run that never got an answer is priced from how its call failed. When
the call policy gave up and every failed attempt carried an HTTP status, the
provider answered each request with an error, and the run is recorded as
costing exactly zero. Google's billing page says a request that fails with a
400 or 500 error is not charged; Anthropic's and OpenAI's error pages say
nothing either way, and the same reading is applied to them. A failure with no
status (a dropped connection or a network timeout) or a call the runner
abandoned at its timeout may still have reached the model and been billed, so
that run's cost stays ``null``.

A cell whose pipeline raised after its run started, in verification, bundling
or the runner's own bookkeeping, is still a ``setup_error`` entry. It keeps the
run's id and the cost its trace records, because the run may have spent money
before the failure.

Budget guard (#196)
    A suite may set ``max_cost_usd``. :class:`BudgetGuard` is asked before
    every run and stops the batch, recorded as ``budget_exhausted`` in the
    summary's ``budget`` block, once the recorded spend of its live runs has
    reached the cap. The stop is recorded by the run that reaches the cap, so a
    summary says so even when that run was the last cell. The check happens
    between runs, so the run that crosses the cap finishes and the overshoot is
    at most one run's cost.

    Only recorded costs count, and an unknown cost is never taken as zero. A
    live run whose model has no price is refused before it starts, and a live
    run that finishes without a cost stops the batch after it. Both are
    recorded as ``budget_unenforceable``, since either could pass the cap
    without the guard seeing it. Fixture and replay runs cost nothing and are
    never refused on price.

    An outside agent (provider ``external``, #210) makes its own model calls,
    which the harness never sees, so its cost is always null. Under a cap it is
    refused before it starts as ``budget_unenforceable``, like an unpriced live
    model. Without a cap it runs as any other config does.

    ``run-suite`` and ``branch`` drive it. ``branch`` builds one guard per
    invocation from the experiment plan's ``budget.max_cost_usd``, shared by
    every condition and seed, and records a budget block on each condition's
    batch (see ``runner/branch.py``). ``run-sweep`` (#198) does not exist yet;
    it is meant to build a ``BudgetGuard`` from its own ``max_cost_usd`` and
    call ``admit`` before and ``charge`` after each run, the same way
    ``BatchRunner.run`` does.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from trace_harness.attribution.schemas import PostBlockOutcome
from trace_harness.environment.control_library import load_library
from trace_harness.models import (
    estimate_cost_usd,
    is_priced,
    makes_live_calls,
    resolve_model_name,
)
from trace_harness.models.cassette import CassetteConfig
from trace_harness.models.policy import LIVE_PROVIDERS
from trace_harness.runner.config import RunConfig
from trace_harness.runner.pipeline import PipelineProgress, PipelineResult, run_task_pipeline
from trace_harness.runner.result import RunStatus
from trace_harness.runner.suite import AgentConfig, SuiteSpec
from trace_harness.runner.target_agent import EXTERNAL_PROVIDER
from trace_harness.tracing.artifact_store import ArtifactStore
from trace_harness.tracing.events import TraceEvent, TraceEventType, utc_now

logger = logging.getLogger(__name__)

# 0.4.0: branch-stage entry fields, summary metadata and the seed of a cell the
# budget never ran (#159); 0.3.0: optional budget block (#196); 0.2.0: per-entry
# verdict, aggregates.incomplete
BATCH_SUMMARY_SCHEMA_VERSION = "0.4.0"

BUDGET_EXHAUSTED = "budget_exhausted"
BUDGET_UNENFORCEABLE = "budget_unenforceable"
BudgetStopReason = Literal["budget_exhausted", "budget_unenforceable"]

# Entry statuses that mean "did not produce a usable, completed run".
_ERROR_STATUSES = ("error", "setup_error")


class BatchRunEntry(BaseModel):
    """One cell of the suite: one task under one agent config."""

    # None when setup failed before a run existed. A setup_error raised after
    # the run started keeps the run's id, and its cost_usd.
    run_id: str | None
    task_id: str
    task_path: str
    task_schema_version: str | None = None  # task "version" for reproducibility
    agent_label: str
    provider: str
    model: str | None = None
    prompt_version: str | None = None
    status: str  # completed / terminated / error / setup_error
    termination_reason: str | None = None
    steps_taken: int | None = None
    verifier_passed: bool | None = None  # None if verify didn't run
    verdict: str | None = None  # pass / fail / incomplete; None if verify didn't run
    verifier_id: str | None = None
    severity: str | None = None
    latency_ms: float | None = None
    cost_usd: float | None = None
    error: str | None = None
    # Filled by the branch stage (#159); None on suite entries and on files
    # written before 0.4.0. ``diverged`` says whether the first action after
    # the fork differed from the recording, and the step says where the run
    # first differed at all.
    condition: str | None = None
    seed: int | None = None
    first_post_fork_divergence_step: int | None = None
    diverged: bool | None = None
    post_block_outcome: PostBlockOutcome | None = None


class BatchAggregates(BaseModel):
    """Roll-up metrics over the batch (pass_rate is over *completed* runs only).

    ``incomplete`` counts runs the verifier looked at but which never reached a
    final answer; they are excluded from ``pass_rate`` and never counted as
    passes. ``terminated``/``errored`` stay as the raw run-status counts.
    """

    total: int
    completed: int
    terminated: int
    errored: int
    incomplete: int = 0
    verifier_passed: int
    verifier_failed: int
    cost_recorded: int
    known_cost_usd: float
    pass_rate: float | None = None  # None when no completed run had a verdict
    by_agent: dict[str, dict[str, int]] = Field(default_factory=dict)


class NotRunCell(BaseModel):
    """A cell the budget guard never started."""

    agent_label: str
    task_path: str
    # The branch stage's cell is a seed (0.4.0); None on suite cells.
    seed: int | None = None


class BatchBudget(BaseModel):
    """What a capped batch spent, and why it stopped early if it did."""

    max_cost_usd: float
    # Sum of the recorded costs of the batch's live runs. Fixture and replay
    # runs cost nothing and add nothing.
    spent_usd: float
    stop_reason: BudgetStopReason | None = None
    detail: str | None = None
    not_run: list[NotRunCell] = Field(default_factory=list)


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
    # Present when the suite set max_cost_usd, and on every branch batch, since
    # an experiment plan always carries one; absent in summaries before 0.3.0.
    budget: BatchBudget | None = None
    # Branch batches record experiment_id, condition, source_run_id and start;
    # empty on suite batches and in summaries before 0.4.0.
    metadata: dict[str, Any] = Field(default_factory=dict)


class BudgetGuard:
    """Refuses to start a run once a batch's live spend has reached its cap.

    Construct one per batch from the spec's ``max_cost_usd``, or one per
    ``branch`` invocation shared by its conditions; None never refuses. Call
    :meth:`admit` before each run and :meth:`charge` after it with the cost its
    entry recorded. The charge that brings spend to the cap records the stop.
    Once stopped, it refuses everything after, so the batch
    stops at that point.
    """

    def __init__(self, max_cost_usd: float | None) -> None:
        self.max_cost_usd = max_cost_usd
        self.spent_usd = 0.0
        self.stop_reason: BudgetStopReason | None = None
        self.detail: str | None = None

    def admit(
        self, provider: str, model: str | None, cassette: CassetteConfig | None = None
    ) -> bool:
        """Whether the next run may start. ``model`` is the resolved model name.

        A run that makes no live call costs nothing, so it is refused only once
        the batch has already stopped. A zero cap therefore still runs fixture
        and replay cells. An outside agent's spend is invisible to the harness,
        so under a cap it is refused as ``budget_unenforceable``.
        """
        if self.max_cost_usd is None:
            return True
        if self.stop_reason is not None:
            return False
        if provider == EXTERNAL_PROVIDER:
            self._stop(
                BUDGET_UNENFORCEABLE,
                "provider external runs an outside agent whose model calls the harness "
                "never sees, so its spend could pass the cap unseen",
            )
            return False
        if not makes_live_calls(provider, cassette):
            return True
        if self.spent_usd >= self.max_cost_usd:
            self._stop(
                BUDGET_EXHAUSTED,
                f"recorded live spend ${self.spent_usd:.6f} reached the "
                f"${self.max_cost_usd:.6f} cap",
            )
            return False
        if not is_priced(provider, model):
            self._stop(
                BUDGET_UNENFORCEABLE,
                f"{provider} model {model!r} has no price, so a run of it could pass the "
                "cap unseen",
            )
            return False
        return True

    def charge(
        self,
        cost_usd: float | None,
        provider: str,
        cassette: CassetteConfig | None = None,
        *,
        run_id: str | None = None,
    ) -> None:
        """Add a finished run's recorded cost.

        A live run with no recorded cost stops the batch, because what it
        spent is unknown. A run that brings the recorded spend to the cap
        stops it as ``budget_exhausted``, so the stop is on record even when no
        cell is left to refuse. A cell that failed before any run existed
        (``run_id`` None) made no provider call and adds nothing.
        """
        if self.max_cost_usd is None or run_id is None:
            return
        if not makes_live_calls(provider, cassette):
            return
        if cost_usd is None:
            if self.stop_reason is None:
                self._stop(
                    BUDGET_UNENFORCEABLE,
                    f"live run {run_id} finished without a recorded cost",
                )
            return
        self.spent_usd = round(self.spent_usd + cost_usd, 6)
        if self.stop_reason is None and self.spent_usd >= self.max_cost_usd:
            self._stop(
                BUDGET_EXHAUSTED,
                f"recorded live spend ${self.spent_usd:.6f} reached the "
                f"${self.max_cost_usd:.6f} cap with live run {run_id}",
            )

    def _stop(self, reason: BudgetStopReason, detail: str) -> None:
        self.stop_reason = reason
        self.detail = detail

    def record(self, not_run: list[NotRunCell]) -> BatchBudget | None:
        """The summary's budget block, or None for an uncapped batch."""
        if self.max_cost_usd is None:
            return None
        return BatchBudget(
            max_cost_usd=self.max_cost_usd,
            spent_usd=self.spent_usd,
            stop_reason=self.stop_reason,
            detail=self.detail,
            not_run=not_run,
        )


def new_batch_id() -> str:
    """Sortable, collision-resistant batch id: batch_<utc timestamp>_<hex8>."""
    return f"batch_{utc_now():%Y%m%dT%H%M%SZ}_{uuid.uuid4().hex[:8]}"


def batch_dir(runs_dir: Path, batch_id: str) -> Path:
    return summary_path(runs_dir, batch_id).parent


def summary_path(runs_dir: Path, batch_id: str) -> Path:
    return ArtifactStore(runs_dir).batch_summary_path(batch_id)


class BatchRunner:
    """Runs a suite over one artifact store, isolating per-cell failures."""

    def __init__(self, store: ArtifactStore, control_library: Path | str | None = None):
        self.store = store
        # Freeze one validated set for the entire batch. Invalid libraries fail
        # before any cell runs instead of becoming a series of setup errors.
        self.controls = (
            load_library(control_library).active_controls() if control_library is not None else None
        )

    def run(self, suite: SuiteSpec) -> BatchSummary:
        started_at = utc_now()
        batch_id = new_batch_id()  # generated before the loop so cells can tag their entries
        entries: list[BatchRunEntry] = []
        guard = BudgetGuard(suite.max_cost_usd)
        not_run: list[NotRunCell] = []
        for config in suite.agent_configs:
            for task_path in suite.tasks:
                if not guard.admit(config.provider, _guard_model(config), config.cassette):
                    not_run.append(NotRunCell(agent_label=config.label, task_path=str(task_path)))
                    continue
                entry = self._run_cell(config, task_path)
                entries.append(entry)
                guard.charge(entry.cost_usd, config.provider, config.cassette, run_id=entry.run_id)
        finished_at = utc_now()

        summary = BatchSummary(
            batch_id=batch_id,
            suite_id=suite.suite_id,
            started_at=started_at,
            finished_at=finished_at,
            agent_configs=suite.agent_configs,
            entries=entries,
            aggregates=aggregate_entries(entries),
            budget=guard.record(not_run),
        )
        self._write_summary(summary)
        self._enrich_index_entries(summary)
        return summary

    def _run_cell(self, config: AgentConfig, task_path: str) -> BatchRunEntry:
        progress = PipelineProgress()
        try:
            result = run_task_pipeline(
                task_path, config, self.store, controls=self.controls, progress=progress
            )
            return entry_from_pipeline(result, config, task_path, self.store.runs_dir)
        except Exception as exc:  # noqa: BLE001 — isolate the cell; the batch goes on
            logger.warning(
                "batch cell failed (agent=%s, task=%s): %s", config.label, task_path, exc
            )
            return attach_started_run(
                _setup_error_entry(config, task_path, exc), progress, self.store.runs_dir
            )

    def _write_summary(self, summary: BatchSummary) -> Path:
        return self.store.write_batch_summary(summary.batch_id, summary)

    def _enrich_index_entries(self, summary: BatchSummary) -> None:
        """Tag completed cells after their authoritative summary is durable."""
        for entry in summary.entries:
            if entry.run_id is None:
                continue
            try:
                self.store.enrich_index_entry_with_batch(entry.run_id, summary.batch_id)
            except Exception:  # noqa: BLE001 — index is rebuildable from the summary
                logger.warning("batch index enrich failed for %s", entry.run_id)


def _trace_events(runs_dir: Path, run_id: str) -> list[TraceEvent] | None:
    """The run's trace, or None when there is no trace that can be read.

    Read through ``ArtifactStore.read_trace``, the one trace parser, because a
    cost is worked out even for a run whose pipeline failed. It already drops a
    final line a hard kill left half written; a trace corrupt anywhere else
    gives no cost.
    """
    try:
        return ArtifactStore(runs_dir).read_trace(run_id)
    except (OSError, ValueError):
        return None


def _nothing_billed(events: list[TraceEvent]) -> bool:
    """Whether a live run that recorded no response spent nothing, as its trace shows.

    True when no request was ever prepared, or when the call policy gave up on
    the run's one call and every failed attempt carried an HTTP status, so the
    provider answered each request with an error (see the module docstring on
    why that is taken as unbilled). False for anything else, including a
    failure with no status (the request may have reached the model before the
    connection dropped), a call the runner abandoned at its timeout, a response
    that arrived and was rejected, and an error with no call record.
    """
    kinds = {event.event_type for event in events}
    if TraceEventType.MODEL_PROMPT not in kinds:
        return True
    errors = [e.payload for e in events if e.event_type is TraceEventType.ERROR]
    if len(errors) != 1 or errors[0].get("kind") != "model_error":
        return False
    record = errors[0].get("call_record")
    if not isinstance(record, dict) or record.get("outcome") == "ok":
        return False
    failures = record.get("failures")
    return isinstance(failures, list) and all(
        isinstance(failure, dict) and isinstance(failure.get("status_code"), int)
        for failure in failures
    )


def _guard_model(config: AgentConfig) -> str | None:
    """The model a live cell would run, for the budget guard's price check.

    Only live providers resolve a model here; the fixture provider needs a
    script to name one and is never checked for a price.
    """
    if not makes_live_calls(config.provider, config.cassette):
        return config.model
    return resolve_model_name(config.provider, config.model, None)


def run_cost_usd(config: RunConfig, runs_dir: Path, run_id: str) -> float | None:
    """What one run cost, read from its trace, or None when that cannot be established.

    The one place a run is priced: a finished cell, a cell whose pipeline
    raised after its run started, and any other stage that runs cells all come
    here, so the same trace always gets the same cost.

    Fixture and cassette-replay runs call no provider, so they cost nothing and
    say so. A live run is priced from the raw provider responses its trace
    recorded as ``model_response`` events, a billed answer the adapter
    rejected included, so the cost comes from the same bytes the trace carries
    rather than from a second accounting path. A provider or model with no
    price stays null. A live run that recorded no response costs zero only when
    its trace shows nothing was billed (see ``_nothing_billed``). Otherwise,
    and when the trace cannot be read, the cost is null, so a live run whose
    cost is unknown is never reported as free.
    """
    if config.provider == "fixture" or (
        config.cassette is not None and config.cassette.mode == "replay"
    ):
        return 0.0
    events = _trace_events(runs_dir, run_id)
    if events is None:
        return None
    responses = [e for e in events if e.event_type is TraceEventType.MODEL_RESPONSE]
    if not responses and config.provider in LIVE_PROVIDERS and _nothing_billed(events):
        return 0.0
    raws = [raw for e in responses if isinstance(raw := e.payload.get("raw"), dict)]
    return estimate_cost_usd(config.provider, config.model, raws)


def attach_started_run(
    entry: BatchRunEntry, progress: PipelineProgress, runs_dir: Path
) -> BatchRunEntry:
    """Point a failed cell's ``setup_error`` entry at its run, if the run had started.

    The run may have been billed before the failure, so the entry keeps the
    run's id, the model it ran and the cost its trace records, priced by
    :func:`run_cost_usd` like a finished cell. A failure before the run leaves
    the entry as it is, since nothing called a provider. ``run-suite`` cells
    and ``branch`` seeds both come through here.
    """
    if progress.run_id is None or progress.run_config is None:
        return entry
    return entry.model_copy(
        update={
            "run_id": progress.run_id,
            "model": progress.run_config.model,
            "cost_usd": run_cost_usd(progress.run_config, runs_dir, progress.run_id),
        }
    )


def entry_from_pipeline(
    result: PipelineResult, config: AgentConfig, task_path: str, runs_dir: Path
) -> BatchRunEntry:
    run = result.run_result
    verifier = result.verifier_result
    latency_ms = round((run.finished_at - run.started_at).total_seconds() * 1000, 1)
    return BatchRunEntry(
        run_id=run.run_id,
        task_id=run.task_id,
        task_path=str(task_path),
        task_schema_version=result.task.schema_version,
        agent_label=config.label,
        provider=result.run_config.provider,
        model=result.run_config.model,
        prompt_version=result.run_config.prompt_version,
        status=str(run.status),
        termination_reason=str(run.termination_reason),
        steps_taken=run.steps_taken,
        verifier_passed=(verifier.passed if verifier is not None else None),
        verdict=(verifier.verdict.value if verifier is not None and verifier.verdict else None),
        verifier_id=(verifier.verifier_id if verifier is not None else None),
        severity=(verifier.severity.value if verifier and verifier.severity else None),
        latency_ms=latency_ms,
        cost_usd=run_cost_usd(result.run_config, runs_dir, run.run_id),
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


def aggregate_entries(entries: list[BatchRunEntry]) -> BatchAggregates:
    completed = [e for e in entries if e.status == str(RunStatus.COMPLETED)]
    terminated = sum(1 for e in entries if e.status == str(RunStatus.TERMINATED))
    # Pass/fail counts consider only completed runs: an incomplete run that
    # recorded no violations is not a genuine pass (mirrors the CLI's CI gate).
    passed = sum(1 for e in completed if e.verifier_passed is True)
    failed = sum(1 for e in completed if e.verifier_passed is False)
    errored = sum(1 for e in entries if e.status in _ERROR_STATUSES)
    incomplete = sum(1 for e in entries if e.verdict == "incomplete")
    recorded_costs = [e.cost_usd for e in entries if e.cost_usd is not None]
    verdicts = passed + failed
    pass_rate = round(passed / verdicts, 4) if verdicts else None

    by_agent: dict[str, dict[str, int]] = {}
    for e in entries:
        bucket = by_agent.setdefault(
            e.agent_label,
            {"passed": 0, "failed": 0, "incomplete": 0, "terminated": 0, "errored": 0},
        )
        if e.verdict == "incomplete":
            bucket["incomplete"] += 1
        if e.status == str(RunStatus.COMPLETED) and e.verifier_passed is True:
            bucket["passed"] += 1
        elif e.status == str(RunStatus.COMPLETED) and e.verifier_passed is False:
            bucket["failed"] += 1
        if e.status == str(RunStatus.TERMINATED):
            bucket["terminated"] += 1
        if e.status in _ERROR_STATUSES:
            bucket["errored"] += 1

    return BatchAggregates(
        total=len(entries),
        completed=len(completed),
        terminated=terminated,
        errored=errored,
        incomplete=incomplete,
        verifier_passed=passed,
        verifier_failed=failed,
        cost_recorded=len(recorded_costs),
        known_cost_usd=round(sum(recorded_costs), 6),
        pass_rate=pass_rate,
        by_agent=by_agent,
    )
