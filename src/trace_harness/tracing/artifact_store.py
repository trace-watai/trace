"""ArtifactStore: the filesystem layout of everything a run produces.

One run, one directory::

    runs/{run_id}/
      task_spec.json            # what was asked (snapshot, replayable)
      run_config.json           # how it was run
      initial_state.json        # the world before
      trace.jsonl               # what happened (one TraceEvent per line)
      final_state.json          # the world after
      run_result.json           # how it ended
      verifier_result.json      # did it actually succeed (written by `verify`)
      attribution_result.json   # where/why it failed (written by `attribute`)
      failure_card.json         # human-readable failure summary (written by `bundle`)
      repair_package.json       # engineering recommendations (written by `bundle`)
      regression_artifact.json  # rerunnable regression spec (written by `bundle`)

The first six are written by the runner; the rest appear as the pipeline
stages run. Partial directories are *valid* — a crashed run keeps whatever
it managed to write, and every file is independently parseable JSON with a
``schema_version`` field.

A runs-dir-level ``index.json`` sits alongside the run directories: one
summary entry per run for cheap listing without scanning every directory. It
is a *derived, rebuildable* convenience (see :meth:`ArtifactStore.rebuild_index`),
not a per-run artifact — so it is deliberately absent from ``ALL_ARTIFACTS``
and exempt from the per-run partial-artifacts promise.

This local-JSON layout *is* the data contract the future API server and
dashboard read (see docs/future_api.md and docs/future_dashboard.md).
Renaming a file here is a breaking
change for them — coordinate.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from trace_harness.tracing.events import TraceEvent
from trace_harness.tracing.recorder import TraceRecorder
from trace_harness.tracing.run_index import RUN_INDEX_SCHEMA_VERSION, RunIndex, RunIndexEntry

# Canonical artifact filenames. Use these constants, never string literals.
TASK_SPEC = "task_spec.json"
RUN_CONFIG = "run_config.json"
INITIAL_STATE = "initial_state.json"
TRACE = "trace.jsonl"
FINAL_STATE = "final_state.json"
RUN_RESULT = "run_result.json"
VERIFIER_RESULT = "verifier_result.json"
ATTRIBUTION_RESULT = "attribution_result.json"
FAILURE_CARD = "failure_card.json"
REPAIR_PACKAGE = "repair_package.json"
REGRESSION_ARTIFACT = "regression_artifact.json"

# Runs-dir-level (not per-run): a derived, rebuildable index of all runs.
RUN_INDEX = "index.json"
BATCHES_DIR = "batches"
BATCH_SUMMARY = "batch_summary.json"

ALL_ARTIFACTS = (
    TASK_SPEC,
    RUN_CONFIG,
    INITIAL_STATE,
    TRACE,
    FINAL_STATE,
    RUN_RESULT,
    VERIFIER_RESULT,
    ATTRIBUTION_RESULT,
    FAILURE_CARD,
    REPAIR_PACKAGE,
    REGRESSION_ARTIFACT,
)


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` so a reader never sees a partial file.

    A plain ``write_text`` interrupted by a crash, kill, or full disk can leave
    a truncated, unparseable artifact — and these JSON files are the data
    contract the verifier, attribution, and dashboard read. Instead we write to
    a temp file in the *same directory* (so the rename stays on one filesystem
    and is atomic), fsync it, then ``os.replace`` it onto the target. The result
    is all-or-nothing: a crash leaves either the previous file or the complete
    new one, never a half-written mix.

    The append-only ``trace.jsonl`` is intentionally exempt — it is written
    incrementally for crash-safe partial traces, and its truncated tail is
    handled on read (see :meth:`TraceRecorder.read_jsonl`).
    """
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


class ArtifactStore:
    """Reads and writes run artifacts under a single runs directory."""

    def __init__(self, runs_dir: Path | str):
        self.runs_dir = Path(runs_dir)

    # --- paths ---

    def run_dir(self, run_id: str) -> Path:
        return self.runs_dir / run_id

    def create_run_dir(self, run_id: str) -> Path:
        path = self.run_dir(run_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def artifact_path(self, run_id: str, name: str) -> Path:
        return self.run_dir(run_id) / name

    def trace_path(self, run_id: str) -> Path:
        return self.artifact_path(run_id, TRACE)

    def exists(self, run_id: str, name: str) -> bool:
        return self.artifact_path(run_id, name).is_file()

    @classmethod
    def for_run_path(cls, run_path: Path | str) -> tuple[ArtifactStore, str]:
        """Resolve a ``runs/{run_id}`` directory into (store, run_id).

        Lets CLI commands accept the path the runner printed, regardless of
        which runs_dir it lives in.
        """
        path = Path(run_path).resolve()
        if not path.is_dir():
            raise FileNotFoundError(f"run directory not found: {path}")
        return cls(path.parent), path.name

    # --- JSON artifacts ---

    def write_json(self, run_id: str, name: str, payload: BaseModel | dict | list) -> Path:
        """Serialize ``payload`` (model or plain data) as pretty-printed JSON.

        Written atomically (see :func:`_atomic_write_text`) so a crash mid-write
        never leaves a truncated artifact for the verifier/dashboard to choke on.
        """
        data = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
        path = self.artifact_path(run_id, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(path, json.dumps(data, indent=2) + "\n")
        return path

    def read_json(self, run_id: str, name: str) -> Any:
        path = self.artifact_path(run_id, name)
        if not path.is_file():
            raise FileNotFoundError(
                f"artifact '{name}' not found for run '{run_id}' (looked in {path}). "
                "Earlier pipeline stages may not have been run yet."
            )
        return json.loads(path.read_text(encoding="utf-8"))

    # --- traces ---

    def read_trace(self, run_id: str) -> list[TraceEvent]:
        path = self.trace_path(run_id)
        if not path.is_file():
            raise FileNotFoundError(f"trace not found for run '{run_id}' (looked in {path})")
        return TraceRecorder.read_jsonl(path)

    # --- listing ---

    def list_runs(self) -> list[str]:
        """Run IDs present on disk, newest-looking last (lexicographic).

        Run IDs embed a UTC timestamp prefix, so lexicographic order is
        chronological order.
        """
        if not self.runs_dir.is_dir():
            return []
        return sorted(p.name for p in self.runs_dir.iterdir() if p.is_dir())

    # --- run index ---

    def index_path(self) -> Path:
        return self.runs_dir / RUN_INDEX

    def read_index(self) -> RunIndex:
        """Load the run index; missing or corrupt indexes are rebuildable."""
        path = self.index_path()
        if not path.is_file():
            return RunIndex()
        try:
            index = RunIndex.model_validate_json(path.read_text(encoding="utf-8"))
        except ValueError:
            return self.rebuild_index()
        if index.schema_version != RUN_INDEX_SCHEMA_VERSION:
            return self.rebuild_index()
        return index

    def upsert_index_entry(self, entry: RunIndexEntry) -> None:
        """Add or replace ``entry`` in the index, keyed by ``run_id``.

        The index is read defensively: missing → start empty; unreadable
        (hand-edited or corrupt) → self-heal by rebuilding from the run
        directories, so a bad index can't silently drop run history. Entries
        stay sorted by ``run_id`` (chronological, like :meth:`list_runs`), and
        the write is atomic (see :func:`_atomic_write_text`).
        """
        try:
            index = self.read_index()
        except ValueError:
            index = self.rebuild_index()
        kept = [e for e in index.entries if e.run_id != entry.run_id]
        kept.append(entry)
        index.entries = sorted(kept, key=lambda e: e.run_id)
        self._write_index(index)

    def enrich_index_entry_with_verifier(self, run_id: str) -> None:
        """Update the index entry for ``run_id`` with the verifier verdict.

        Reads the existing entry and ``verifier_result.json`` (if present),
        sets ``verifier_passed`` and ``failed_check_count``, and re-upserts
        atomically. If the index entry is missing but ``run_result.json`` is
        present, it is reconstructed first. A missing verifier or run result
        remains a safe no-op — the verifier stage guards this call anyway.

        Reads only the raw JSON fields it needs so ``tracing/`` stays decoupled
        from ``verifiers/`` at runtime (no VerifierResult import here).
        """
        if not self.exists(run_id, VERIFIER_RESULT):
            return
        index = self.read_index()
        existing = next((e for e in index.entries if e.run_id == run_id), None)
        if existing is None:
            if not self.exists(run_id, RUN_RESULT):
                return
            try:
                existing = RunIndexEntry.model_validate(self.read_json(run_id, RUN_RESULT))
            except (FileNotFoundError, ValueError):
                return
        verifier_fields = self._read_verifier_index_fields(run_id)
        if verifier_fields is None:
            return
        updated = existing.model_copy(
            update={
                "verifier_passed": verifier_fields[0],
                "failed_check_count": verifier_fields[1],
            }
        )
        self.upsert_index_entry(updated)

    def enrich_index_entry_with_batch(self, run_id: str, batch_id: str) -> None:
        """Set ``batch_id`` on the run's index entry.

        Called by :class:`BatchRunner` after each cell completes. A missing
        entry is a safe no-op — the batch summary is the authoritative source
        for batch membership; this field is a convenience for cheap filtering.
        """
        index = self.read_index()
        existing = next((e for e in index.entries if e.run_id == run_id), None)
        if existing is None:
            return
        self.upsert_index_entry(existing.model_copy(update={"batch_id": batch_id}))

    def rebuild_index(self) -> RunIndex:
        """Reconstruct the index from run artifacts and batch summaries.

        Runs without a result (crashed before finalize) are skipped. Each entry
        is enriched with the verifier verdict when ``verifier_result.json``
        exists and with batch membership when a batch summary references it.
        The result is written back atomically and returned.
        """
        batch_memberships = self._read_batch_memberships()
        entries: list[RunIndexEntry] = []
        for run_id in self.list_runs():
            if not self.exists(run_id, RUN_RESULT):
                continue
            try:
                entry = RunIndexEntry.model_validate(self.read_json(run_id, RUN_RESULT))
            except (FileNotFoundError, ValueError):
                continue
            verifier_fields = self._read_verifier_index_fields(run_id)
            if verifier_fields is not None:
                entry = entry.model_copy(
                    update={
                        "verifier_passed": verifier_fields[0],
                        "failed_check_count": verifier_fields[1],
                    }
                )
            batch_id = batch_memberships.get(run_id)
            if batch_id is not None:
                entry = entry.model_copy(update={"batch_id": batch_id})
            entries.append(entry)
        index = RunIndex(entries=sorted(entries, key=lambda e: e.run_id))
        self._write_index(index)
        return index

    def _read_batch_memberships(self) -> dict[str, str]:
        """Map run ids to batch ids from valid persisted batch summaries."""
        batches_dir = self.runs_dir / BATCHES_DIR
        if not batches_dir.is_dir():
            return {}

        memberships: dict[str, str] = {}
        for path in sorted(batches_dir.glob(f"*/{BATCH_SUMMARY}")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            batch_id = data.get("batch_id")
            entries = data.get("entries")
            if not isinstance(batch_id, str) or not isinstance(entries, list):
                continue
            for item in entries:
                if not isinstance(item, dict):
                    continue
                run_id = item.get("run_id")
                if isinstance(run_id, str):
                    memberships[run_id] = batch_id
        return memberships

    def _read_verifier_index_fields(self, run_id: str) -> tuple[bool, int] | None:
        """Read only validated verdict fields without importing verifier models."""
        if not self.exists(run_id, VERIFIER_RESULT):
            return None
        try:
            data = self.read_json(run_id, VERIFIER_RESULT)
        except (FileNotFoundError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        passed = data.get("passed")
        failed_checks = data.get("failed_checks")
        if not isinstance(passed, bool) or not isinstance(failed_checks, list):
            return None
        return passed, len(failed_checks)

    def _write_index(self, index: RunIndex) -> None:
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(self.index_path(), index.model_dump_json(indent=2) + "\n")
