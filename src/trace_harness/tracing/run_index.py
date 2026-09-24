"""RunIndex: a runs-dir-level summary so consumers can list runs cheaply.

One entry per run, denormalized from each run's ``run_result.json``, optional
``verifier_result.json``, and persisted batch summaries. The index is a
*derived convenience* — it can always be rebuilt from those artifacts (see
:meth:`ArtifactStore.rebuild_index`), so a missing or corrupt index is
recoverable, never fatal.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from trace_harness.runner.config import RunConfig
    from trace_harness.runner.result import RunResult

# 0.6.0: bundle_key (#211); 0.5.0: provider/model; 0.4.0: verdict
RUN_INDEX_SCHEMA_VERSION = "0.6.0"


class RunIndexEntry(BaseModel):
    """A one-line summary of a finished run, mirrored from its RunResult.

    ``status``/``termination_reason`` are plain strings (RunResult uses StrEnums)
    so the index stays decoupled from the runner module — the values are
    identical on the wire.

    ``verifier_passed``/``failed_check_count`` are populated by
    :meth:`ArtifactStore.enrich_index_entry_with_verifier` after the verify
    stage runs; they are ``None`` until then.

    ``batch_id`` is set by :meth:`ArtifactStore.enrich_index_entry_with_batch`
    when a run is part of a suite batch. Rebuilds recover it from persisted
    batch summaries; it is ``None`` for single runs.

    ``bundle_key`` is set by the bundle stage (#211) on every run it bundled.
    Rebuilds recover it from ``failure_card.json``; it is ``None`` for
    unbundled runs and for runs bundled before 0.6.0.
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
    # "pass" | "fail" | "incomplete"; None until the verify stage ran. A run
    # whose status is not "completed" is "incomplete" regardless of what the
    # verifier file says, so pre-0.4.0 files rebuild correctly.
    verdict: str | None = None
    # Which model actually produced the run. Without these a reader cannot tell
    # a live provider run from a scripted fixture run, which is the whole point
    # of retaining live-provider evidence. ``model`` is None for providers that
    # do not name one (the fixture provider replays a script).
    provider: str | None = None
    model: str | None = None
    batch_id: str | None = None
    bundle_key: str | None = None

    @classmethod
    def from_result(cls, result: RunResult, config: RunConfig | None = None) -> RunIndexEntry:
        """Build an entry from a finished run.

        ``config`` supplies ``provider``/``model``. It is optional because
        ``rebuild_index`` reconstructs entries from artifacts instead and reads
        those two fields from ``run_config.json`` directly.
        """
        return cls(
            provider=config.provider if config else None,
            model=config.model if config else None,
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
