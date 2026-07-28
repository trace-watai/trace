"""RunIndex: a runs-dir-level summary so consumers can list runs cheaply.

One entry per run, denormalized from each run's ``run_result.json`` and (when
available) its ``verifier_result.json``. The index is a *derived convenience*
— it can always be rebuilt by scanning the run directories (see
:meth:`ArtifactStore.rebuild_index`), so a missing or corrupt index is
recoverable, never fatal.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from trace_harness.runner.result import RunResult

RUN_INDEX_SCHEMA_VERSION = "0.2.0"


class RunIndexEntry(BaseModel):
    """A one-line summary of a finished run, mirrored from its RunResult.

    ``status``/``termination_reason`` are plain strings (RunResult uses StrEnums)
    so the index stays decoupled from the runner module — the values are
    identical on the wire.

    ``verifier_passed``/``failed_check_count`` are populated by
    :meth:`ArtifactStore.enrich_index_entry_with_verifier` after the verify
    stage runs; they are ``None`` until then.
    """

    run_id: str
    task_id: str
    status: str
    termination_reason: str
    steps_taken: int
    started_at: datetime
    finished_at: datetime
    error: str | None = None
    verifier_passed: bool | None = None
    failed_check_count: int | None = None

    @classmethod
    def from_result(cls, result: RunResult) -> RunIndexEntry:
        return cls(
            run_id=result.run_id,
            task_id=result.task_id,
            status=result.status,
            termination_reason=result.termination_reason,
            steps_taken=result.steps_taken,
            started_at=result.started_at,
            finished_at=result.finished_at,
            error=result.error,
        )


class RunIndex(BaseModel):
    schema_version: str = RUN_INDEX_SCHEMA_VERSION
    entries: list[RunIndexEntry] = Field(default_factory=list)
