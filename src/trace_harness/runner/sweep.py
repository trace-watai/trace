"""run-sweep (#198): every task of a suite under every live provider, for every seed.

A sweep spec (``fixtures/sweeps/*.json``) names a suite, the live providers and
models to run it under, the seeds, and a spend cap. :func:`run_sweep` runs each
cell (one task under one provider for one seed) through the same path a
``run-suite`` cell takes, with the model calls recorded to cassettes under
``runs/sweeps/{sweep_id}/cassettes/``. Each provider's cells are written as one
ordinary batch, tagged with the sweep in ``BatchSummary.metadata`` and with the
seed on each entry, and the sweep's roll-up goes to
``runs/sweeps/{sweep_id}/sweep_summary.json`` (see ``runner/sweep_summary.py``).

Order and budget
    Cells run seed by seed, and within a seed provider by provider over every
    task. One :class:`~trace_harness.runner.batch.BudgetGuard` spans the whole
    sweep, so a cap reached early stops the sweep inside one seed. Every
    earlier seed is complete for every provider. In the seed where it stopped,
    the providers whose turn came before the stop have it complete, the one
    running at the stop has it partly run, and the rest never started it, so
    providers' complete seeds differ by at most one. The spec refuses a zero
    cap when it loads. Each provider's adapter is built once
    before any cell runs, so a missing key stops the sweep before anything is
    spent, and the guard is asked once per provider, so an unpriced model does
    too. After that the guard's contract is the batch's own. It admits each
    cell before it starts and is charged the cell's recorded cost after, and a
    live run that finishes without a cost stops the sweep.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from trace_harness.models import create_model_adapter
from trace_harness.models.cassette import CassetteConfig
from trace_harness.models.policy import LIVE_PROVIDERS, CallPolicy
from trace_harness.runner.batch import (
    BatchBudget,
    BatchRunEntry,
    BatchRunner,
    BatchSummary,
    BudgetGuard,
    NotRunCell,
    aggregate_entries,
    new_batch_id,
)
from trace_harness.runner.suite import AgentConfig, load_suite
from trace_harness.runner.sweep_summary import (
    SWEEP_CASSETTES,
    ProviderBatch,
    SweepSummary,
    completed_with,
    load_staging,
    summarize_sweep,
)
from trace_harness.tracing import artifact_store as names
from trace_harness.tracing.artifact_store import ArtifactStore, _atomic_write_text
from trace_harness.tracing.events import utc_now
from trace_harness.verifiers.base import VerifierResult

logger = logging.getLogger(__name__)

SWEEP_SPEC_SCHEMA_VERSION = "0.1.0"
SWEEPS_DIR = "sweeps"
SWEEP_SUMMARY = "sweep_summary.json"


class SweepProvider(BaseModel):
    """One live model to run every task under."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    provider: str
    model: str = Field(min_length=1)
    temperature: float | None = None
    max_steps: int = Field(default=16, ge=1)
    timeout_seconds: float = Field(default=120.0, gt=0)
    prompt_version: str | None = None
    call_policy: CallPolicy | None = None

    @field_validator("provider")
    @classmethod
    def _live(cls, value: str) -> str:
        if value not in LIVE_PROVIDERS:
            raise ValueError(f"{value!r} makes no live call; a sweep runs live providers only")
        return value

    def agent_config(self, seed: int | None = None, cassettes: Path | None = None) -> AgentConfig:
        """The provider as an agent config; per seed and recording when both are given."""
        return AgentConfig(
            label=self.label if seed is None else f"{self.label}-seed{seed}",
            provider=self.provider,
            model=self.model,
            prompt_version=self.prompt_version,
            temperature=self.temperature,
            seed=seed,
            max_steps=self.max_steps,
            timeout_seconds=self.timeout_seconds,
            cassette=(
                None
                if cassettes is None
                else CassetteConfig(mode="record", directory=str(cassettes))
            ),
            call_policy=self.call_policy,
        )


class SweepSpec(BaseModel):
    """What one sweep runs, and what it may spend."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1.0"] = SWEEP_SPEC_SCHEMA_VERSION
    sweep_name: str = Field(min_length=1)
    description: str | None = None
    suite: str = Field(min_length=1)
    seeds: list[int] = Field(min_length=1)
    providers: list[SweepProvider] = Field(min_length=1)
    # Required, since every cell of a sweep calls a provider. A zero cap would
    # run nothing, so it is refused as a malformed spec.
    max_cost_usd: float = Field(gt=0)
    budget_note: str | None = None

    @model_validator(mode="after")
    def _distinct(self) -> SweepSpec:
        # Recordings are keyed by task, model and seed, so two providers on one
        # model would write to the same cassette.
        for name, values in (
            ("seeds", self.seeds),
            ("provider labels", [p.label for p in self.providers]),
            ("models", [p.model for p in self.providers]),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"sweep {name} must be distinct: {values}")
        return self


class SweepLoadError(ValueError):
    """A sweep spec was missing or malformed (a CLI input error)."""


def load_sweep(path: Path | str) -> SweepSpec:
    p = Path(path)
    if not p.is_file():
        raise SweepLoadError(f"sweep spec not found: {p}")
    try:
        return SweepSpec.model_validate(json.loads(p.read_text(encoding="utf-8")))
    except ValueError as exc:
        raise SweepLoadError(f"invalid sweep spec ({p}): {exc}") from exc


def new_sweep_id() -> str:
    return f"sweep_{utc_now():%Y%m%dT%H%M%SZ}_{uuid.uuid4().hex[:8]}"


def sweep_dir(runs_dir: Path, sweep_id: str) -> Path:
    return Path(runs_dir) / SWEEPS_DIR / sweep_id


@dataclass
class _ProviderCells:
    batch_id: str
    entries: list[BatchRunEntry] = field(default_factory=list)
    not_run: list[NotRunCell] = field(default_factory=list)
    spent_usd: float = 0.0


def run_sweep(
    spec: SweepSpec, store: ArtifactStore, *, spec_path: str | None = None
) -> SweepSummary:
    """Run every cell, write one batch per provider and the sweep summary."""
    suite = load_suite(spec.suite)
    staging = load_staging(suite.tasks)
    for provider in spec.providers:
        # Raises ProviderNotConfiguredError on a missing key. Makes no call.
        create_model_adapter(provider.provider, model=provider.model)

    sweep_id = new_sweep_id()
    cassettes = sweep_dir(store.runs_dir, sweep_id) / SWEEP_CASSETTES
    started_at = utc_now()
    guard = BudgetGuard(spec.max_cost_usd)
    runner = BatchRunner(store)
    cells = {p.label: _ProviderCells(new_batch_id()) for p in spec.providers}
    stopped_by: str | None = None

    def stop_seen(label: str) -> None:
        nonlocal stopped_by
        if guard.stop_reason is not None and stopped_by is None:
            stopped_by = label

    for provider in spec.providers:
        guard.admit(provider.provider, provider.model, provider.agent_config(1, cassettes).cassette)
        stop_seen(provider.label)
    for seed in spec.seeds:
        for provider in spec.providers:
            config, own = provider.agent_config(seed, cassettes), cells[provider.label]
            for task_path in suite.tasks:
                if not guard.admit(config.provider, config.model, config.cassette):
                    stop_seen(provider.label)
                    own.not_run.append(
                        NotRunCell(agent_label=config.label, task_path=task_path, seed=seed)
                    )
                    continue
                # The batch's own cell path, so a sweep cell and a suite cell
                # are the same run with the same failure isolation.
                entry = runner.run_cell(config, task_path).model_copy(update={"seed": seed})
                own.entries.append(entry)
                before = guard.spent_usd
                guard.charge(entry.cost_usd, config.provider, config.cassette, run_id=entry.run_id)
                own.spent_usd += guard.spent_usd - before
                stop_seen(provider.label)

    batches = []
    for provider in spec.providers:
        own = cells[provider.label]
        stopped = bool(own.not_run) or stopped_by == provider.label
        batch = BatchSummary(
            batch_id=own.batch_id,
            suite_id=suite.suite_id,
            started_at=started_at,
            finished_at=utc_now(),
            agent_configs=[provider.agent_config(seed, cassettes) for seed in spec.seeds],
            entries=own.entries,
            aggregates=aggregate_entries(own.entries),
            budget=BatchBudget(
                max_cost_usd=spec.max_cost_usd,
                spent_usd=round(own.spent_usd, 6),
                stop_reason=guard.stop_reason if stopped else None,
                detail=guard.detail if stopped else None,
                not_run=own.not_run,
            ),
            metadata={
                "sweep_id": sweep_id,
                "sweep_name": spec.sweep_name,
                "provider_label": provider.label,
            },
        )
        store.write_batch_summary(batch.batch_id, batch)
        _tag_runs(store, batch)
        batches.append(ProviderBatch(provider.agent_config(), batch))

    summary = summarize_sweep(
        sweep_id=sweep_id,
        sweep_name=spec.sweep_name,
        spec_path=spec_path,
        suite_id=suite.suite_id,
        task_paths=list(suite.tasks),
        seeds=spec.seeds,
        batches=batches,
        staging=staging,
        failed_checks=_failed_checks(store, batches),
        budget=guard.record([cell for c in cells.values() for cell in c.not_run]),
        started_at=started_at,
        finished_at=utc_now(),
    )
    write_sweep_summary(store.runs_dir, summary)
    return summary


def write_sweep_summary(runs_dir: Path, summary: SweepSummary) -> Path:
    path = sweep_dir(runs_dir, summary.sweep_id) / SWEEP_SUMMARY
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, summary.model_dump_json(indent=2) + "\n")
    return path


def _failed_checks(store: ArtifactStore, batches: list[ProviderBatch]) -> dict[str, Any]:
    failing = [
        entry.run_id
        for item in batches
        for entry in item.batch.entries
        if completed_with(entry, "fail") and entry.run_id
    ]
    return {
        run_id: VerifierResult.model_validate(
            store.read_json(run_id, names.VERIFIER_RESULT)
        ).failed_checks
        for run_id in failing
    }


def _tag_runs(store: ArtifactStore, batch: BatchSummary) -> None:
    for entry in batch.entries:
        if entry.run_id is None:
            continue
        try:
            store.enrich_index_entry_with_batch(entry.run_id, batch.batch_id)
        except Exception:  # noqa: BLE001 (the summary is the source of truth)
            logger.warning("batch index enrich failed for %s", entry.run_id)
