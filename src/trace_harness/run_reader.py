"""RunReader: one typed read interface over the runs/ artifact store.

Every consumer — the CLI today, Skye's dashboard and CI tomorrow — should read
stored runs through this service instead of scraping ``runs/{run_id}/`` files
by hand. It returns the **existing artifact schemas unchanged** (no reshaping;
see docs/future_api.md), so the contract is the same JSON the pipeline wrote.

This is the documented Python service the future FastAPI app will wrap; the
endpoint map in docs/future_api.md is mirrored 1:1 by the methods here:

    GET /runs                       -> list_runs()        -> list[RunSummary]
    GET /runs/{id}                  -> get_run(id)        -> RunResult
    GET /runs/{id}/task             -> get_task(id)       -> TaskSpec
    GET /runs/{id}/trace            -> get_trace(id)      -> list[TraceEvent]
    GET /runs/{id}/verifier         -> get_verifier(id)   -> VerifierResult | None
    GET /runs/{id}/attribution      -> get_attribution(id)-> AttributionResult | None
    GET /runs/{id}/bundle           -> get_bundle(id)     -> FailureBundle | None

Missing-artifact states are explicit:
  * unknown run id (no directory)              -> raise RunNotFound
  * a not-yet-produced downstream artifact     -> return None
    (verifier/attribution before those stages run; bundle before `bundle`)
  * a foundational artifact missing from an     -> FileNotFoundError from
    existing run dir (run_result/task/trace)       ArtifactStore.read_json,
                                                    with its stage guidance

``list_runs`` reads one run-index file instead of one artifact per run and
includes the verifier verdict when the verify stage has run.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from trace_harness.attribution.schemas import AttributionResult
from trace_harness.failure_bundles.generator import FailureBundle
from trace_harness.failure_bundles.schemas import FailureCard, RepairPackage
from trace_harness.regression.schemas import RegressionArtifact
from trace_harness.runner.result import RunResult
from trace_harness.tasks.schemas import TaskSpec
from trace_harness.tracing import artifact_store as names
from trace_harness.tracing.artifact_store import ArtifactStore
from trace_harness.tracing.events import TraceEvent
from trace_harness.tracing.run_index import RunIndexEntry
from trace_harness.verifiers.base import VerifierResult


class RunNotFound(FileNotFoundError):
    """Raised when a run id has no directory under the runs dir.

    Subclasses FileNotFoundError so the CLI's input-error handler reports it
    cleanly (exit 2) while programmatic consumers can still catch it precisely.
    """


class RunSummary(BaseModel):
    """One-line summary of a finished run for listing.

    ``status``/``termination_reason`` are plain strings (the source uses
    StrEnums; the value on the wire is identical). ``verifier_passed`` and
    ``failed_check_count`` are ``None`` until the verify stage has run.
    """

    run_id: str
    task_id: str
    status: str
    termination_reason: str
    steps_taken: int
    started_at: str
    finished_at: str
    error: str | None = None
    verifier_passed: bool | None = None
    failed_check_count: int | None = None

    @classmethod
    def from_result(cls, result: RunResult) -> RunSummary:
        return cls(
            run_id=result.run_id,
            task_id=result.task_id,
            status=str(result.status),
            termination_reason=str(result.termination_reason),
            steps_taken=result.steps_taken,
            started_at=result.started_at.isoformat(),
            finished_at=result.finished_at.isoformat(),
            error=result.error,
        )

    @classmethod
    def from_entry(cls, entry: RunIndexEntry) -> RunSummary:
        return cls(
            run_id=entry.run_id,
            task_id=entry.task_id,
            status=entry.status,
            termination_reason=entry.termination_reason,
            steps_taken=entry.steps_taken,
            started_at=entry.started_at.isoformat(),
            finished_at=entry.finished_at.isoformat(),
            error=entry.error,
            verifier_passed=entry.verifier_passed,
            failed_check_count=entry.failed_check_count,
        )


class RunReader:
    """Typed reads over a single runs directory."""

    def __init__(self, store: ArtifactStore):
        self.store = store

    @classmethod
    def from_runs_dir(cls, runs_dir: Path | str) -> RunReader:
        return cls(ArtifactStore(runs_dir))

    # --- listing ---

    def list_runs(self) -> list[RunSummary]:
        """Summaries of every listable run, oldest-first (chronological).

        Reads one run-index file and includes the verifier verdict when the
        verify stage has run. A cheap directory/stat reconciliation detects
        missing or stale entries and rebuilds from source artifacts.
        """
        index = self.store.read_index()
        listable_run_ids = {
            run_id
            for run_id in self.store.list_runs()
            if self.store.exists(run_id, names.RUN_RESULT)
        }
        indexed_run_ids = {entry.run_id for entry in index.entries}
        if indexed_run_ids != listable_run_ids:
            index = self.store.rebuild_index()
        return [RunSummary.from_entry(entry) for entry in index.entries]

    # --- single run ---

    def get_run(self, run_id: str) -> RunResult:
        self._require_run(run_id)
        return RunResult.model_validate(self.store.read_json(run_id, names.RUN_RESULT))

    def get_task(self, run_id: str) -> TaskSpec:
        self._require_run(run_id)
        return TaskSpec.model_validate(self.store.read_json(run_id, names.TASK_SPEC))

    def get_trace(self, run_id: str) -> list[TraceEvent]:
        self._require_run(run_id)
        return self.store.read_trace(run_id)

    def get_verifier(self, run_id: str) -> VerifierResult | None:
        return self._read_optional(run_id, names.VERIFIER_RESULT, VerifierResult)

    def get_attribution(self, run_id: str) -> AttributionResult | None:
        return self._read_optional(run_id, names.ATTRIBUTION_RESULT, AttributionResult)

    def get_bundle(self, run_id: str) -> FailureBundle | None:
        """The three bundle artifacts, or None if the run hasn't been bundled.

        The ``bundle`` stage writes all three together, so they are present or
        absent as a set; a partial bundle (crash mid-stage) surfaces as a
        FileNotFoundError rather than a silently half-built bundle.
        """
        self._require_run(run_id)
        if not self.store.exists(run_id, names.FAILURE_CARD):
            return None
        return FailureBundle(
            failure_card=FailureCard.model_validate(
                self.store.read_json(run_id, names.FAILURE_CARD)
            ),
            repair_package=RepairPackage.model_validate(
                self.store.read_json(run_id, names.REPAIR_PACKAGE)
            ),
            regression_artifact=RegressionArtifact.model_validate(
                self.store.read_json(run_id, names.REGRESSION_ARTIFACT)
            ),
        )

    # --- internals ---

    def _require_run(self, run_id: str) -> None:
        if not self.store.run_dir(run_id).is_dir():
            raise RunNotFound(f"run not found: '{run_id}' (looked in {self.store.run_dir(run_id)})")

    def _read_optional(self, run_id: str, name: str, model: type[BaseModel]) -> BaseModel | None:
        self._require_run(run_id)
        if not self.store.exists(run_id, name):
            return None
        return model.model_validate(self.store.read_json(run_id, name))
