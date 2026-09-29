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
      bundle_ref.json           # pointer to an earlier run's card (written by `bundle`)

The first six are written by the runner; the rest appear as the pipeline
stages run. Partial directories are *valid* — a crashed run keeps whatever
it managed to write, and every file is independently parseable JSON with a
``schema_version`` field.

A failing run holds either the three bundle files or, when its bundle key
matched an earlier card, only ``bundle_ref.json`` naming the run that holds
them (#211).

A runs-dir-level ``index.json`` sits alongside the run directories: one
summary entry per run for cheap listing without scanning every directory. It
is a *derived, rebuildable* convenience (see :meth:`ArtifactStore.rebuild_index`),
not a per-run artifact — so it is deliberately absent from ``ALL_ARTIFACTS``
and exempt from the per-run partial-artifacts promise. Every index write and
the bundle stage hold one advisory lock, ``.bundle.lock`` beside the index
(see :meth:`ArtifactStore.bundle_lock`).

This local-JSON layout *is* the data contract the future API server and
dashboard read (see docs/future_api.md and docs/future_dashboard.md).
Renaming a file here is a breaking
change for them — coordinate.
"""

from __future__ import annotations

import errno
import json
import os
import tempfile
import threading
from collections.abc import Callable, Collection, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path, PurePath, PurePosixPath
from typing import IO, Any

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
# Written by ``replay --apply-control`` rather than the run pipeline, so it is
# deliberately absent from ``ALL_ARTIFACTS``.
REPAIR_VALIDATION = "repair_validation.json"
# Written by ``bundle`` in place of the three bundle files when the run
# reproduces an earlier card (#211). A run holds one or the other, so it is
# absent from ``ALL_ARTIFACTS`` too.
BUNDLE_REF = "bundle_ref.json"

# Runs-dir-level (not per-run): a derived, rebuildable index of all runs.
RUN_INDEX = "index.json"
# Runs-dir-level, held by every index write and while the bundle stage looks a
# key up and writes (#211).
BUNDLE_LOCK = ".bundle.lock"
EXPERIMENTS_DIR = "experiments"
EXPERIMENT_SPEC = "experiment.json"
EXPERIMENT_RESULT = "result.json"
EXPERIMENT_REPORT_MD = "report.md"

BATCHES_DIR = "batches"
BATCH_SUMMARY = "batch_summary.json"
REGRESSION_GATE_SUMMARY = "regression_gate_summary.json"
# Per-batch (not per-run): the check/category/coverage report over a batch,
# derived from the batch summary + each run's artifacts (see runner/report.py).
SUITE_REPORT = "suite_report.json"
SUITE_REPORT_MD = "suite_report.md"

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


def safe_run_dir_name(run_id: str) -> str:
    """Return ``run_id`` when it names a directory beside other runs, else raise.

    Pointers between runs are followed by joining a run id onto the runs
    directory, so a value with a separator or a parent reference could read
    outside it.
    """
    if (
        not run_id
        or run_id in {".", ".."}
        or PurePath(run_id).name != run_id
        or "/" in run_id
        or "\\" in run_id
        or ":" in run_id
    ):
        raise ValueError(f"not a run directory name: {run_id!r}")
    return run_id


# The errno msvcrt.locking raises when LK_LOCK has tried for about ten seconds
# and another process still holds the lock (EDEADLOCK in the CRT _locking
# reference). Every other errno is a real failure, such as a bad handle.
LOCK_CONTENDED_ERRNO = getattr(errno, "EDEADLOCK", errno.EDEADLK)


def retry_while_contended(attempt: Callable[[], None]) -> None:
    """Call ``attempt`` until it returns, retrying only while the lock is contended.

    An ``OSError`` with :data:`LOCK_CONTENDED_ERRNO` means another process
    still holds the lock, so waiting longer is right. Any other ``OSError`` is
    raised at once, where retrying it would spin forever.
    """
    while True:
        try:
            attempt()
            return
        except OSError as exc:
            if exc.errno != LOCK_CONTENDED_ERRNO:
                raise


if os.name == "nt":  # pragma: no cover - exercised on Windows only
    import msvcrt

    def _lock(handle: IO[bytes]) -> None:
        handle.seek(0)
        retry_while_contended(lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1))

    def _unlock(handle: IO[bytes]) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(handle: IO[bytes]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)

    def _unlock(handle: IO[bytes]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


# How deep each thread is in each runs directory's bundle lock, keyed by the
# lock file's resolved path. The file lock itself is not reentrant (a second
# open of the same file in one process waits for the first), so a thread that
# already holds it only counts the nesting.
_held_locks = threading.local()


def _lock_depths() -> dict[str, int]:
    depths = getattr(_held_locks, "depths", None)
    if depths is None:
        depths = _held_locks.depths = {}
    return depths


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


def atomic_write_text(path: Path, text: str) -> None:
    """The same all-or-nothing write, for a file the store does not own.

    ``experiment freeze`` rewrites a plan in place with it (#195).
    """
    _atomic_write_text(path, text)


def _with_verdict(entry: RunIndexEntry, fields: tuple[bool, int, str | None]) -> RunIndexEntry:
    """Apply verifier fields to an index entry, deriving the three-state verdict.

    The run's own ``status`` wins: a run that did not complete is
    ``incomplete`` even if an old verifier file says ``passed: true``, and
    ``verifier_passed`` is forced to False for it so nothing counting passes
    is fooled by a pre-0.4.0 artifact.
    """
    passed, failed_count, file_verdict = fields
    if entry.status != "completed":
        verdict, passed = "incomplete", False
    else:
        verdict = file_verdict or ("pass" if passed else "fail")
    return entry.model_copy(
        update={"verifier_passed": passed, "failed_check_count": failed_count, "verdict": verdict}
    )


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

    def batch_summary_path(self, batch_id: str) -> Path:
        return self.runs_dir / BATCHES_DIR / batch_id / BATCH_SUMMARY

    def suite_report_path(self, batch_id: str) -> Path:
        return self.runs_dir / BATCHES_DIR / batch_id / SUITE_REPORT

    def suite_report_md_path(self, batch_id: str) -> Path:
        return self.runs_dir / BATCHES_DIR / batch_id / SUITE_REPORT_MD

    # --- experiments (#155) ---
    #
    # An experiment lives beside the batches it compares, outside all of them,
    # because it is the thing that relates several batches.

    def experiment_dir(self, experiment_id: str) -> Path:
        """Refuses an id that is not one plain path segment.

        Experiment ids come from hand-written plan files, so an id like
        ``../../x`` would otherwise read or write outside the runs directory.
        The plan model enforces the full id pattern; this is the last check
        before a path is built.
        """
        segment = PurePosixPath(experiment_id)
        if (
            str(segment) != experiment_id
            or len(segment.parts) != 1
            or experiment_id in {".", ".."}
            or "\\" in experiment_id
        ):
            raise ValueError(f"not a valid experiment id: {experiment_id!r}")
        return self.runs_dir / EXPERIMENTS_DIR / experiment_id

    def experiment_spec_path(self, experiment_id: str) -> Path:
        return self.experiment_dir(experiment_id) / EXPERIMENT_SPEC

    def experiment_result_path(self, experiment_id: str) -> Path:
        return self.experiment_dir(experiment_id) / EXPERIMENT_RESULT

    def experiment_report_path(self, experiment_id: str) -> Path:
        return self.experiment_dir(experiment_id) / EXPERIMENT_REPORT_MD

    def write_experiment_spec(self, experiment_id: str, payload: BaseModel | dict) -> Path:
        """Persist the plan. Written before any condition runs."""
        return self._write_experiment_json(self.experiment_spec_path(experiment_id), payload)

    def write_experiment_result(
        self, experiment_id: str, payload: BaseModel | dict, *, markdown: str | None = None
    ) -> Path:
        """Persist the result, and the human-readable report beside it."""
        path = self._write_experiment_json(self.experiment_result_path(experiment_id), payload)
        if markdown is not None:
            _atomic_write_text(self.experiment_report_path(experiment_id), markdown)
        return path

    def _write_experiment_json(self, path: Path, payload: BaseModel | dict) -> Path:
        data = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(path, json.dumps(data, indent=2) + "\n")
        return path

    def read_experiment_spec(self, experiment_id: str) -> Any:
        return self._read_experiment_json(self.experiment_spec_path(experiment_id), experiment_id)

    def read_experiment_result(self, experiment_id: str) -> Any:
        return self._read_experiment_json(self.experiment_result_path(experiment_id), experiment_id)

    def _read_experiment_json(self, path: Path, experiment_id: str) -> Any:
        if not path.is_file():
            raise FileNotFoundError(
                f"{path.name} not found for experiment '{experiment_id}' (looked in {path})."
            )
        return json.loads(path.read_text(encoding="utf-8"))

    def list_experiments(self) -> list[str]:
        """Experiment ids that have a plan on disk, sorted.

        Generated ids sort by creation time. Hand-named ones such as
        ``exp_000_baseline`` sort by name.
        """
        root = self.runs_dir / EXPERIMENTS_DIR
        if not root.is_dir():
            return []
        return sorted(d.name for d in root.iterdir() if (d / EXPERIMENT_SPEC).is_file())

    def write_batch_summary(self, batch_id: str, payload: BaseModel | dict) -> Path:
        """Atomically persist the authoritative summary for one batch."""
        data = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
        path = self.batch_summary_path(batch_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(path, json.dumps(data, indent=2) + "\n")
        return path

    def read_batch_summary(self, batch_id: str) -> Any:
        """Load one batch's summary JSON, or raise with the path we looked in."""
        path = self.batch_summary_path(batch_id)
        if not path.is_file():
            raise FileNotFoundError(
                f"batch summary not found for batch '{batch_id}' (looked in {path})."
            )
        return json.loads(path.read_text(encoding="utf-8"))

    def write_suite_report(
        self, batch_id: str, payload: BaseModel | dict, *, markdown: str | None = None
    ) -> Path:
        """Atomically persist a batch's suite report (JSON, plus optional markdown).

        The markdown string is rendered by the caller (``runner/report.py``) so
        this layer keeps no view logic. Returns the JSON path.
        """
        data = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
        path = self.suite_report_path(batch_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(path, json.dumps(data, indent=2) + "\n")
        if markdown is not None:
            _atomic_write_text(self.suite_report_md_path(batch_id), markdown)
        return path

    def read_suite_report(self, batch_id: str) -> Any:
        """Load one batch's suite report JSON, or raise with a generation hint."""
        path = self.suite_report_path(batch_id)
        if not path.is_file():
            raise FileNotFoundError(
                f"suite report not found for batch '{batch_id}' (looked in {path}). "
                "Run `trace-harness report-suite <batch_id>` to generate it."
            )
        return json.loads(path.read_text(encoding="utf-8"))

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

        The read and the write happen under :meth:`bundle_lock`, as every index
        write does, so a run finishing in one process cannot write back an
        index read before another process recorded a bundle key or a verdict.
        """
        with self.bundle_lock():
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
        with self.bundle_lock():
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
            self.upsert_index_entry(_with_verdict(existing, verifier_fields))

    # --- failure bundles (#211) ---

    @contextmanager
    def bundle_lock(self) -> Iterator[None]:
        """Hold the runs directory's lock across a key lookup and its writes.

        Two processes bundling into one runs directory would otherwise both miss
        a card and both write one, or both rewrite a card and lose a run from
        its occurrences. Every index write takes the same lock, so an index
        read by one process is never written back over a bundle key, verdict
        or batch id that another process recorded in between.

        The lock is an advisory lock on ``.bundle.lock`` beside ``index.json``.
        The operating system releases it when the holder exits, so a crash
        never leaves the directory locked. It is reentrant within a thread,
        which lets the bundle stage write the index while it holds the lock.
        There is only the one lock, so no two locks can be taken in opposite
        orders.
        """
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        path = self.runs_dir / BUNDLE_LOCK
        key = str(path.resolve())
        depths = _lock_depths()
        if depths.get(key):
            depths[key] += 1
            try:
                yield
            finally:
                depths[key] -= 1
            return
        with path.open("a+b") as handle:
            _lock(handle)
            depths[key] = 1
            try:
                yield
            finally:
                del depths[key]
                _unlock(handle)

    def holds_finished_bundle(self, run_id: str, bundle_key: str) -> bool:
        """Whether ``run_id`` holds a card for ``bundle_key`` with the rest of its bundle.

        The bundle stage writes the repair package and the regression artifact
        before the card, so a card marks a finished bundle. A card found
        without the other two was left by hand or by an older writer and is
        never joined.
        """
        return (
            self._card_bundle_key(run_id) == bundle_key
            and self.exists(run_id, REPAIR_PACKAGE)
            and self.exists(run_id, REGRESSION_ARTIFACT)
        )

    def find_bundle_card(self, bundle_key: str, scope: Collection[str] | None = None) -> str | None:
        """The run whose directory holds the finished bundle for ``bundle_key``, or None.

        ``scope`` limits the lookup to those run ids, for a caller that wants
        one card per key within a batch or an experiment instead of the whole
        runs directory. The branch stage, for one, can keep each condition's
        cards apart by passing the runs of that condition. A scoped lookup
        reads each named run's card directly, in run id order, and skips the
        index. None, the default, searches every run in the directory as
        follows.

        The index nominates candidates, tried in run id order so a duplicate
        left by an older writer resolves the same way every time, and a
        candidate counts only when :meth:`holds_finished_bundle` agrees. An
        index that has fallen behind the run directories is rebuilt first, the
        same reconciliation ``RunReader.list_runs`` does.

        The index is a derived convenience and can lack a key the card has,
        for instance when it was edited by hand or written without the lock by
        a tool or an older version. So a miss falls back to scanning every
        ``failure_card.json`` for the key before
        it answers None, and a card found that way has its key written back to
        its index entry. Call this under :meth:`bundle_lock`, as the bundle
        stage does, so that no card can appear between the scan and the
        caller's write.
        """
        if scope is not None:
            for run_id in sorted({safe_run_dir_name(run_id) for run_id in scope}):
                if self.holds_finished_bundle(run_id, bundle_key):
                    return run_id
            return None
        index = self.read_index()
        listable = {run_id for run_id in self.list_runs() if self.exists(run_id, RUN_RESULT)}
        if {entry.run_id for entry in index.entries} != listable:
            index = self.rebuild_index()
        tried = set()
        for entry in index.entries:
            if entry.bundle_key != bundle_key:
                continue
            tried.add(entry.run_id)
            if self.holds_finished_bundle(entry.run_id, bundle_key):
                return entry.run_id
        for path in sorted(self.runs_dir.glob(f"*/{FAILURE_CARD}")):
            run_id = path.parent.name
            if run_id not in tried and self.holds_finished_bundle(run_id, bundle_key):
                self.set_index_bundle_key(run_id, bundle_key)
                return run_id
        return None

    def bundle_home(self, run_id: str) -> str | None:
        """The run whose directory holds the bundle covering ``run_id``.

        That is ``run_id`` itself when it holds a failure card, the run its
        ``bundle_ref.json`` names when it reproduced an earlier card, and None
        when it was never bundled. A pointer that does not load, or names
        anything but a sibling run directory, raises ValueError naming the run.
        """
        if self.exists(run_id, FAILURE_CARD):
            return run_id
        if not self.exists(run_id, BUNDLE_REF):
            return None
        try:
            data = self.read_json(run_id, BUNDLE_REF)
        except ValueError as exc:
            raise ValueError(f"{BUNDLE_REF} for run '{run_id}' does not load: {exc}") from None
        canonical = data.get("canonical_run_id") if isinstance(data, dict) else None
        if not isinstance(canonical, str):
            raise ValueError(f"{BUNDLE_REF} for run '{run_id}' names no canonical_run_id")
        try:
            return safe_run_dir_name(canonical)
        except ValueError as exc:
            raise ValueError(
                f"{BUNDLE_REF} for run '{run_id}' names no usable run: {exc}"
            ) from None

    def bundle_homes(self, run_ids: Iterable[str]) -> dict[str, str]:
        """Map each bundled run in ``run_ids`` to the run whose directory holds its bundle.

        Runs never bundled are left out. A caller that copies a set of runs
        somewhere else, such as sweep retention or public results staging,
        compares the values with its set to find the homes it would otherwise
        leave behind, since a reproduction's card, repair package and
        regression artifact live only in its home.
        """
        homes = {}
        for run_id in run_ids:
            home = self.bundle_home(run_id)
            if home is not None:
                homes[run_id] = home
        return homes

    def set_index_bundle_key(self, run_id: str, bundle_key: str) -> None:
        """Record the run's bundle key on its index entry.

        A missing entry is recovered by rebuilding the index from the run
        directories first. A run with no ``run_result.json`` has no entry to
        carry the key, and stays out of the index as it would anyway.
        """
        with self.bundle_lock():
            index = self.read_index()
            existing = next((e for e in index.entries if e.run_id == run_id), None)
            if existing is None:
                index = self.rebuild_index()
                existing = next((e for e in index.entries if e.run_id == run_id), None)
                if existing is None:
                    return
            self.upsert_index_entry(existing.model_copy(update={"bundle_key": bundle_key}))

    def enrich_index_entry_with_batch(self, run_id: str, batch_id: str) -> None:
        """Set ``batch_id`` on the run's index entry.

        Called by :class:`BatchRunner` after each cell completes. A missing
        entry is a safe no-op — the batch summary is the authoritative source
        for batch membership; this field is a convenience for cheap filtering.
        """
        with self.bundle_lock():
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
        The result is written back atomically and returned, under
        :meth:`bundle_lock` so no index write lands between the scan and the
        write.
        """
        with self.bundle_lock():
            return self._rebuild_index()

    def _rebuild_index(self) -> RunIndex:
        batch_memberships = self._read_batch_memberships()
        entries: list[RunIndexEntry] = []
        for run_id in self.list_runs():
            if not self.exists(run_id, RUN_RESULT):
                continue
            try:
                entry = RunIndexEntry.model_validate(self.read_json(run_id, RUN_RESULT))
            except (FileNotFoundError, ValueError):
                continue
            config_fields = self._read_config_index_fields(run_id)
            if config_fields is not None:
                entry = entry.model_copy(
                    update={"provider": config_fields[0], "model": config_fields[1]}
                )
            verifier_fields = self._read_verifier_index_fields(run_id)
            if verifier_fields is not None:
                entry = _with_verdict(entry, verifier_fields)
            batch_id = batch_memberships.get(run_id)
            if batch_id is not None:
                entry = entry.model_copy(update={"batch_id": batch_id})
            bundle_key = self._read_bundle_index_field(run_id)
            if bundle_key is not None:
                entry = entry.model_copy(update={"bundle_key": bundle_key})
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

    def _read_json_field(self, run_id: str, name: str, field: str) -> str | None:
        """One string field of a run artifact, or None when absent or unreadable."""
        if not self.exists(run_id, name):
            return None
        try:
            data = self.read_json(run_id, name)
        except (FileNotFoundError, ValueError):
            return None
        value = data.get(field) if isinstance(data, dict) else None
        return value if isinstance(value, str) else None

    def _card_bundle_key(self, run_id: str) -> str | None:
        return self._read_json_field(run_id, FAILURE_CARD, "bundle_key")

    def _read_bundle_index_field(self, run_id: str) -> str | None:
        """The run's bundle key from its card or its pointer, without importing either model.

        None for unbundled runs and for cards written before failure card 0.5.0.
        """
        return self._card_bundle_key(run_id) or self._read_json_field(
            run_id, BUNDLE_REF, "bundle_key"
        )

    def _read_config_index_fields(self, run_id: str) -> tuple[str, str | None] | None:
        """Read ``(provider, model)`` from run_config.json without importing RunConfig.

        Mirrors :meth:`_read_verifier_index_fields` so a rebuilt index matches
        what the runner wrote. Returns None when the config is missing or
        malformed, leaving both fields null rather than guessing.
        """
        if not self.exists(run_id, RUN_CONFIG):
            return None
        try:
            data = self.read_json(run_id, RUN_CONFIG)
        except (FileNotFoundError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        provider = data.get("provider")
        if not isinstance(provider, str):
            return None
        model = data.get("model")
        return provider, model if isinstance(model, str) else None

    def _read_verifier_index_fields(self, run_id: str) -> tuple[bool, int, str | None] | None:
        """Read only validated verdict fields without importing verifier models.

        Returns ``(passed, failed_check_count, verdict)``; ``verdict`` is None
        for files written before verifier schema 0.4.0.
        """
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
        verdict = data.get("verdict")
        return passed, len(failed_checks), verdict if isinstance(verdict, str) else None

    def _write_index(self, index: RunIndex) -> None:
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(self.index_path(), index.model_dump_json(indent=2) + "\n")
