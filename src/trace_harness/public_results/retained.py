"""Lay the retained tree out as one runs directory that RunReader can read.

Retained evidence under ``docs/acceptance/`` is spread over several folders
with slightly different layouts. Runs sit in ``runs/``, in dated folders beside
it, in folders nested inside it and inside experiment folders (the fork points
of a branch experiment). One batch summary sits loose in ``runs/`` under its
own name, another under ``batches/{batch_id}/``. Experiments sit under
``experiments/``. RunReader reads a single runs directory, so
:func:`stage_retained` copies every retained item into a fresh one:

    {dest}/{run_id}/...                           every file of the run dir
    {dest}/batches/{batch_id}/batch_summary.json  (and suite_report.json beside it)
    {dest}/experiments/{experiment_id}/...        the experiment folder's own files

Nothing is interpreted beyond what the layout needs. A run dir is a directory
holding ``run_result.json``. An experiment is a directory holding
``experiment.json``. Only the files directly in it are its own, and the walk
goes on into its subfolders, so the runs retained inside it are staged as runs.
A batch summary is a file named ``batch_summary.json`` or ending in
``_batch_summary.json``, and its ``batch_id`` names the destination.

Index files are never copied or read. RunReader rebuilds the index of the
staged copy from the run artifacts, so the upload does not depend on the index
or its format, and reading never rewrites a tracked index in place, which
RunReader would otherwise do for the retained ``index.json`` that predates
index schema 0.5.0.

A run that reproduced an earlier failure card holds ``bundle_ref.json`` in
place of its own card, repair package and regression artifact (#211). The
pointer is read here, and :attr:`StagedSet.bundle_refs` records the run it
names, whose row then carries the card alone. A pointer to a run that is not
retained, or to one that holds no card, is refused with both run ids named,
because the hosted row would otherwise show a bundled failure as unbundled. A
run that holds a card of its own is its own bundle home, whatever pointer sits
beside it, as ``ArtifactStore.bundle_home`` reads it.

Two sources claiming the same id is an error. Nothing is ever overwritten.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from trace_harness.tracing import artifact_store as names

BATCH_SUMMARY_SUFFIX = "_" + names.BATCH_SUMMARY
# Written by the bundle stage since #211 when a run's failure matched an
# earlier card. The constant is repeated here because this module reads the
# file as plain JSON and must work on trees written before and after #211.
BUNDLE_REF = "bundle_ref.json"


@dataclass
class StagedSet:
    """Where each staged item came from, relative to the retained root."""

    runs_dir: Path
    runs: dict[str, str] = field(default_factory=dict)
    batches: dict[str, str] = field(default_factory=dict)
    experiments: dict[str, str] = field(default_factory=dict)
    # Run id of a reproduction -> the run whose directory holds the failure
    # card covering it, read from the reproduction's bundle_ref.json (#211).
    bundle_refs: dict[str, str] = field(default_factory=dict)

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


def _canonical_run_id(path: Path, run_id: str) -> str:
    """The ``canonical_run_id`` a run's ``bundle_ref.json`` names, checked."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"unreadable {BUNDLE_REF} for run '{run_id}': {exc}") from None
    canonical = data.get("canonical_run_id") if isinstance(data, dict) else None
    if (
        not isinstance(canonical, str)
        or canonical in {"", ".", "..", run_id}
        or any(sep in canonical for sep in ("/", "\\", ":"))
    ):
        raise ValueError(
            f"{BUNDLE_REF} for run '{run_id}' names no usable canonical_run_id (got {canonical!r})"
        )
    return canonical


def _check_bundle_refs(staged: StagedSet) -> None:
    for run_id, canonical in sorted(staged.bundle_refs.items()):
        where = f"run '{run_id}' ({staged.runs[run_id]})"
        if canonical not in staged.runs:
            raise ValueError(
                f"{where} holds a {BUNDLE_REF} naming run '{canonical}' as the home of its "
                f"failure card, and '{canonical}' is not retained. Retain that run directory "
                f"as well, or re-bundle '{run_id}' so it holds its own card."
            )
        if not (staged.runs_dir / canonical / names.FAILURE_CARD).is_file():
            raise ValueError(
                f"{where} holds a {BUNDLE_REF} naming run '{canonical}' "
                f"({staged.runs[canonical]}) as the home of its failure card, and "
                f"'{canonical}' holds no {names.FAILURE_CARD}."
            )


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
        children.sort()
        if names.RUN_RESULT in files:
            _claim(staged.runs, "run", here.name, source)
            shutil.copytree(here, dest / here.name)
            if BUNDLE_REF in files and names.FAILURE_CARD not in files:
                staged.bundle_refs[here.name] = _canonical_run_id(here / BUNDLE_REF, here.name)
            children[:] = []
            continue
        if names.EXPERIMENT_SPEC in files:
            _claim(staged.experiments, "experiment", here.name, source)
            target = dest / names.EXPERIMENTS_DIR / here.name
            target.mkdir(parents=True)
            for name in sorted(files):
                if name != names.RUN_INDEX:
                    shutil.copyfile(here / name, target / name)
            continue
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
    _check_bundle_refs(staged)
    return staged
