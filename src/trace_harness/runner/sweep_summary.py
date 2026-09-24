"""SweepSummary: what one live sweep found, per task, provider and failing cell (#198).

A sweep (``runner/sweep.py``) runs every task of a suite under several live
providers for several seeds, and each provider's cells form one ordinary batch.
This module rolls those batches into one summary. It reports how many seeds
passed each task, how many tasks flipped between seeds, what the sweep cost and
what each verified failure cost, and every failing cell with a label.

Counting
    A cell passed or failed only when its run completed with verdict ``pass``
    or ``fail``. Anything else that ran (terminated, errored, verdict
    ``incomplete``) counts as incomplete, and a cell the budget never started
    counts as not run. A task flipped under a provider when at least one seed
    passed and at least one failed. Incomplete seeds never make a flip.

    A verified failure is a failing cell with at least one check whose
    ``blocks_release`` is true, as ``verified_failure_count`` is defined in
    ``docs/methodology_metrics.md``. Cost per verified failure divides the
    sweep's recorded cost by that count. It is null when nothing failed, and
    null when any run is missing its cost, since an unknown cost is never zero.

Labels
    Every failing cell is ``staged_trap`` or ``natural``. A cell is a staged
    trap when its task is a staged negative and every check that fired is one
    the task stages. Anything else is natural. A task is a staged negative when
    it has a pinned expectation under ``fixtures/expected/`` and no task in the
    suite names it as a positive sibling. It stages the checks its pinned
    expectation lists, and any check the attributor files under a failure
    category the task lists in ``targeted_failure_modes``. So a failure on a
    valid task or a positive sibling is natural, and so is a staged negative's
    failure on a check its author never aimed at. ``docs/live_sweep.md`` gives
    the reasons for the rule.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

# The attributor's own check table, so a label and an attribution never file
# the same check under different categories.
from trace_harness.attribution.heuristic import _CHECK_CATEGORY
from trace_harness.models.cassette import CassetteRequestConfig, cassette_path
from trace_harness.runner.batch import BatchBudget, BatchRunEntry, BatchSummary
from trace_harness.runner.config import PROMPT_VERSION
from trace_harness.runner.suite import AgentConfig
from trace_harness.tasks.loader import load_task
from trace_harness.verifiers.base import FailedCheck

SWEEP_SUMMARY_SCHEMA_VERSION = "0.1.0"
STAGED_TRAP = "staged_trap"
NATURAL = "natural"
FailureLabel = Literal["staged_trap", "natural"]

#: Where pinned expectations live, relative to the repository root, the way
#: suite task paths are.
PINNED_EXPECTATIONS_DIR = Path("fixtures/expected")
#: The recordings of a sweep, under its directory.
SWEEP_CASSETTES = "cassettes"


class SweepTaskRow(BaseModel):
    """One task under one provider, counted over the sweep's seeds."""

    provider_label: str
    task_id: str
    task_path: str
    passed: int = 0
    failed: int = 0
    incomplete: int = 0
    not_run: int = 0
    flipped: bool = False


class SweepFailingCell(BaseModel):
    """One run that completed with verdict ``fail``, and how it is labeled."""

    provider_label: str
    provider: str
    model: str
    seed: int
    task_id: str
    task_path: str
    run_id: str
    batch_id: str
    failed_check_ids: list[str]
    # At least one failed check blocks release, so this is a verified failure.
    blocking: bool
    label: FailureLabel
    # The checks that made the cell natural; empty on a staged trap.
    natural_check_ids: list[str] = Field(default_factory=list)
    label_reason: str
    cost_usd: float | None = None
    # Relative to the sweep directory, where replay finds the recording.
    cassette_path: str


class SweepProviderResult(BaseModel):
    """One provider's batch, counted over every task and seed."""

    label: str
    provider: str
    model: str
    batch_id: str
    runs: int
    passed: int
    failed: int
    incomplete: int
    not_run: int
    flipped_tasks: int
    verified_failures: int
    cost_usd: float
    cost_recorded: int


class SweepSummary(BaseModel):
    """The result of one sweep, written to ``runs/sweeps/{sweep_id}/``."""

    schema_version: str = SWEEP_SUMMARY_SCHEMA_VERSION
    sweep_id: str
    sweep_name: str
    spec_path: str | None = None
    suite_id: str
    started_at: datetime
    finished_at: datetime
    seeds: list[int]
    task_count: int
    providers: list[SweepProviderResult]
    tasks: list[SweepTaskRow]
    # Distinct tasks that flipped under at least one provider.
    flipped_tasks: int
    runs: int
    cost_usd: float
    cost_recorded: int
    verified_failures: int
    natural_verified_failures: int
    cost_per_verified_failure: float | None = None
    cost_per_natural_verified_failure: float | None = None
    failing_cells: list[SweepFailingCell] = Field(default_factory=list)
    budget: BatchBudget | None = None


@dataclass(frozen=True)
class TaskStaging:
    """What one suite task stages, read from the task and its pinned expectation."""

    task_id: str
    pinned_checks: frozenset[str]
    targeted_modes: frozenset[str]
    positive_sibling: bool

    @property
    def staged_negative(self) -> bool:
        return bool(self.pinned_checks) and not self.positive_sibling

    def stages(self, check_id: str) -> bool:
        if not self.staged_negative:
            return False
        if check_id in self.pinned_checks:
            return True
        category = _CHECK_CATEGORY.get(check_id)
        return category is not None and category.value in self.targeted_modes


def load_staging(
    task_paths: Iterable[str], expected_dir: Path = PINNED_EXPECTATIONS_DIR
) -> dict[str, TaskStaging]:
    """Each suite task's staging, keyed by the path the suite lists it under.

    Loading every task here also means a malformed task stops a sweep before
    anything is spent.
    """
    tasks = {path: load_task(Path(path)) for path in task_paths}
    siblings = {
        Path(str(sibling.get("task_fixture", ""))).resolve()
        for task in tasks.values()
        for sibling in task.metadata.get("positive_sibling_tasks") or []
        if isinstance(sibling, dict)
    }
    staging = {}
    for path, task in tasks.items():
        pinned = expected_dir / f"{task.task_id}_expected_verifier.json"
        checks = (
            json.loads(pinned.read_text(encoding="utf-8"))["expected"]["failed_check_ids"]
            if pinned.is_file()
            else []
        )
        staging[path] = TaskStaging(
            task_id=task.task_id,
            pinned_checks=frozenset(checks),
            targeted_modes=frozenset(task.targeted_failure_modes),
            positive_sibling=Path(path).resolve() in siblings,
        )
    return staging


def label_failure(
    staging: TaskStaging, failed_check_ids: Iterable[str]
) -> tuple[FailureLabel, list[str], str]:
    """The label, the checks that made it natural, and a one-line reason."""
    fired = sorted(set(failed_check_ids))
    unstaged = [check for check in fired if not staging.stages(check)]
    if fired and not unstaged:
        return STAGED_TRAP, [], "Every check that fired is one the task stages."
    if staging.staged_negative:
        return NATURAL, unstaged, f"The task does not stage {', '.join(unstaged)}."
    if staging.positive_sibling:
        return NATURAL, fired, "The task is a positive sibling that must keep passing."
    return NATURAL, fired, "The task stages no failure."


@dataclass(frozen=True)
class ProviderBatch:
    """One provider of a sweep and the batch its cells wrote."""

    config: AgentConfig
    batch: BatchSummary


def summarize_sweep(
    *,
    sweep_id: str,
    sweep_name: str,
    spec_path: str | None,
    suite_id: str,
    task_paths: list[str],
    seeds: list[int],
    batches: list[ProviderBatch],
    staging: Mapping[str, TaskStaging],
    failed_checks: Mapping[str, list[FailedCheck]],
    budget: BatchBudget | None,
    started_at: datetime,
    finished_at: datetime,
) -> SweepSummary:
    """Roll provider batches into one summary.

    ``failed_checks`` maps the run id of every failing cell to the checks its
    verifier result records.
    """
    providers, rows, cells = [], [], []
    all_entries: list[BatchRunEntry] = []
    for item in batches:
        config, batch = item.config, item.batch
        entries = batch.entries
        all_entries += entries
        not_run = batch.budget.not_run if batch.budget else []
        by_task = {
            path: SweepTaskRow(
                provider_label=config.label, task_id=staging[path].task_id, task_path=path
            )
            for path in task_paths
        }
        for entry in entries:
            row = by_task[entry.task_path]
            if _completed_with(entry, "pass"):
                row.passed += 1
            elif _completed_with(entry, "fail"):
                row.failed += 1
            else:
                row.incomplete += 1
        for cell in not_run:
            by_task[cell.task_path].not_run += 1
        for row in by_task.values():
            row.flipped = row.passed > 0 and row.failed > 0
        failing = [
            _failing_cell(config, batch.batch_id, entry, staging, failed_checks[entry.run_id])
            for entry in entries
            if _completed_with(entry, "fail") and entry.run_id is not None
        ]
        costs = [entry.cost_usd for entry in entries if entry.cost_usd is not None]
        providers.append(
            SweepProviderResult(
                label=config.label,
                provider=config.provider,
                model=str(config.model),
                batch_id=batch.batch_id,
                runs=len(entries),
                passed=sum(row.passed for row in by_task.values()),
                failed=sum(row.failed for row in by_task.values()),
                incomplete=sum(row.incomplete for row in by_task.values()),
                not_run=len(not_run),
                flipped_tasks=sum(row.flipped for row in by_task.values()),
                verified_failures=sum(cell.blocking for cell in failing),
                cost_usd=round(sum(costs), 6),
                cost_recorded=len(costs),
            )
        )
        rows += by_task.values()
        cells += sorted(failing, key=lambda c: (c.seed, task_paths.index(c.task_path)))

    costs = [entry.cost_usd for entry in all_entries if entry.cost_usd is not None]
    cost_usd = round(sum(costs), 6)
    # A cell whose setup failed never called a provider, so only runs need a cost.
    priced = all(e.cost_usd is not None for e in all_entries if e.run_id is not None)
    verified = [cell for cell in cells if cell.blocking]
    natural = [cell for cell in verified if cell.label == NATURAL]
    return SweepSummary(
        sweep_id=sweep_id,
        sweep_name=sweep_name,
        spec_path=spec_path,
        suite_id=suite_id,
        started_at=started_at,
        finished_at=finished_at,
        seeds=seeds,
        task_count=len(task_paths),
        providers=providers,
        tasks=rows,
        flipped_tasks=len({row.task_path for row in rows if row.flipped}),
        runs=len(all_entries),
        cost_usd=cost_usd,
        cost_recorded=len(costs),
        verified_failures=len(verified),
        natural_verified_failures=len(natural),
        cost_per_verified_failure=cost_per(cost_usd, len(verified), priced),
        cost_per_natural_verified_failure=cost_per(cost_usd, len(natural), priced),
        failing_cells=cells,
        budget=budget,
    )


def cost_per(cost_usd: float, count: int, priced: bool) -> float | None:
    """Cost divided by a count, or null when the count is zero or a cost is unknown."""
    return round(cost_usd / count, 6) if count and priced else None


def cell_cassette_path(config: AgentConfig, task_id: str, seed: int) -> str:
    """Where a cell's recording sits under the sweep directory.

    Built from the same request fields the cassette recorder keys on, so a
    replay with these knobs finds exactly this file.
    """
    request = CassetteRequestConfig(
        task_id=task_id,
        provider=config.provider,
        model=str(config.model),
        temperature=config.temperature,
        seed=seed,
        timeout_seconds=config.timeout_seconds,
        prompt_version=config.prompt_version or PROMPT_VERSION,
    )
    return cassette_path(SWEEP_CASSETTES, request).as_posix()


def _completed_with(entry: BatchRunEntry, verdict: str) -> bool:
    return entry.status == "completed" and entry.verdict == verdict


def _failing_cell(
    config: AgentConfig,
    batch_id: str,
    entry: BatchRunEntry,
    staging: Mapping[str, TaskStaging],
    checks: list[FailedCheck],
) -> SweepFailingCell:
    assert entry.run_id is not None and entry.seed is not None
    fired = sorted({check.check_id for check in checks})
    label, natural_checks, reason = label_failure(staging[entry.task_path], fired)
    return SweepFailingCell(
        provider_label=config.label,
        provider=config.provider,
        model=str(entry.model),
        seed=entry.seed,
        task_id=entry.task_id,
        task_path=entry.task_path,
        run_id=entry.run_id,
        batch_id=batch_id,
        failed_check_ids=fired,
        blocking=any(check.blocks_release for check in checks),
        label=label,
        natural_check_ids=natural_checks,
        label_reason=reason,
        cost_usd=entry.cost_usd,
        cassette_path=cell_cassette_path(config, entry.task_id, entry.seed),
    )
