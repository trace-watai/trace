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

This local-JSON layout *is* the data contract the future API server and
dashboard read (see docs/future_api.md and docs/future_dashboard.md).
Renaming a file here is a breaking
change for them — coordinate.

# TODO(Samrath/tracing): a run index file for cheap run listing once run
# volume makes directory scans annoying. (Atomic writes: done — write_json
# goes through a same-dir temp file + os.replace.)
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
