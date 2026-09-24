"""Lay the retained tree out as one runs directory that RunReader can read.

Retained evidence under ``docs/acceptance/`` is spread over several folders
with slightly different layouts. Runs sit in ``runs/``, in dated folders beside
it and in folders nested inside it. One batch summary sits loose in ``runs/``
under its own name, another under ``batches/{batch_id}/``. Experiments sit
under ``experiments/``. RunReader reads a single runs directory, so
:func:`stage_retained` copies every retained item into a fresh one:

    {dest}/{run_id}/...                           every file of the run dir
    {dest}/batches/{batch_id}/batch_summary.json  (and suite_report.json beside it)
    {dest}/experiments/{experiment_id}/...        experiment.json, result.json, report.md

Nothing is interpreted beyond what the layout needs. A run dir is a directory
holding ``run_result.json``. An experiment is a directory holding
``experiment.json``. A batch summary is a file named ``batch_summary.json`` or
ending in ``_batch_summary.json``, and its ``batch_id`` names the destination.

Index files are never copied or read. RunReader rebuilds the index of the
staged copy from the run artifacts, so the upload does not depend on the index
format (#213 may replace it) and reading never rewrites a tracked index in
place, which RunReader would otherwise do for the retained ``index.json`` that
predates index schema 0.5.0.

Two sources claiming the same id is an error, never a silent overwrite.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from trace_harness.tracing import artifact_store as names

BATCH_SUMMARY_SUFFIX = "_" + names.BATCH_SUMMARY


@dataclass
class StagedSet:
    """Where each staged item came from, relative to the retained root."""

    runs_dir: Path
    runs: dict[str, str] = field(default_factory=dict)
    batches: dict[str, str] = field(default_factory=dict)
    experiments: dict[str, str] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not (self.runs or self.batches or self.experiments)


def _claim(seen: dict[str, str], kind: str, item_id: str, source: str) -> None:
    if item_id in seen:
        raise ValueError(f"{kind} '{item_id}' is retained twice: {seen[item_id]} and {source}")
    seen[item_id] = source


def _batch_id(path: Path) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"unreadable batch summary {path}: {exc}") from None
    batch_id = data.get("batch_id") if isinstance(data, dict) else None
    if not isinstance(batch_id, str) or not batch_id or "/" in batch_id:
        raise ValueError(f"batch summary {path} has no usable batch_id")
    return batch_id


def stage_retained(root: Path | str, dest: Path | str) -> StagedSet:
    """Copy every retained run, batch and experiment under ``root`` into ``dest``."""
    root = Path(root)
    dest = Path(dest)
    if not root.is_dir():
        raise ValueError(f"retained root not found: {root}")
    if dest.exists() and any(dest.iterdir()):
        raise ValueError(f"staging directory is not empty: {dest}")
    dest.mkdir(parents=True, exist_ok=True)
    staged = StagedSet(runs_dir=dest)

    def unreadable(error: OSError) -> None:
        raise error

    for directory, children, files in os.walk(root, onerror=unreadable, followlinks=False):
        here = Path(directory)
        source = here.relative_to(root).as_posix()
        if names.RUN_RESULT in files:
            _claim(staged.runs, "run", here.name, source)
            shutil.copytree(here, dest / here.name)
            children[:] = []
            continue
        if names.EXPERIMENT_SPEC in files:
            _claim(staged.experiments, "experiment", here.name, source)
            shutil.copytree(here, dest / names.EXPERIMENTS_DIR / here.name)
            children[:] = []
            continue
        children.sort()
        for name in sorted(files):
            if name != names.BATCH_SUMMARY and not name.endswith(BATCH_SUMMARY_SUFFIX):
                continue
            path = here / name
            batch_id = _batch_id(path)
            _claim(staged.batches, "batch", batch_id, path.relative_to(root).as_posix())
            target = dest / names.BATCHES_DIR / batch_id
            target.mkdir(parents=True)
            shutil.copyfile(path, target / names.BATCH_SUMMARY)
            report = here / names.SUITE_REPORT
            if name == names.BATCH_SUMMARY and report.is_file():
                shutil.copyfile(report, target / names.SUITE_REPORT)
    return staged
