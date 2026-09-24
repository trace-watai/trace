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
    GET /batches/{id}               -> get_batch_summary(id) -> BatchSummary
    GET /batches/{id}/report        -> get_suite_report(id)  -> SuiteReport

Two bundle readers have no endpoint yet. ``get_bundle_ref`` returns the
pointer a reproduction holds (#211) and ``get_occurrences`` the runs its card
covers, for callers that copy runs elsewhere and have to keep each
reproduction with the run holding its card.

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
from trace_harness.failure_bundles.schemas import (
    BundleOccurrence,
    BundleRef,
    FailureCard,
    RepairPackage,
)
from trace_harness.regression.schemas import RegressionArtifact
from trace_harness.runner.batch import BatchSummary
from trace_harness.runner.experiment import ExperimentResult, ExperimentSpec, load_plan
from trace_harness.runner.report import SuiteReport, build_suite_report
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
    ``batch_id`` is set only for runs that came from a suite batch.
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
    # "pass" | "fail" | "incomplete"; None until verified.
    verdict: str | None = None
    # Which model produced the run; None for pre-0.5.0 index files.
    provider: str | None = None
    model: str | None = None
    batch_id: str | None = None
    # The failure card this run belongs to (#211); None until bundled.
    bundle_key: str | None = None

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
            verdict=entry.verdict,
            provider=entry.provider,
            model=entry.model,
            batch_id=entry.batch_id,
            bundle_key=entry.bundle_key,
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

    def list_runs_for_batch(self, batch_id: str) -> list[RunSummary]:
        """All runs tagged with ``batch_id``, oldest-first (chronological)."""
        return [s for s in self.list_runs() if s.batch_id == batch_id]

    # --- batches ---

    def get_batch_summary(self, batch_id: str) -> BatchSummary:
        """The authoritative summary for one batch (raises if the batch is unknown)."""
        return BatchSummary.model_validate(self.store.read_batch_summary(batch_id))

    def get_suite_report(self, batch_id: str) -> SuiteReport:
        """The persisted suite report for one batch.

        Reads ``suite_report.json`` unchanged, exactly like the other getters.
        If it has not been generated yet, falls back to building it in memory
        from the batch summary + run artifacts (no write) so a caller never has
        to sequence a ``report-suite`` first.
        """
        try:
            return SuiteReport.model_validate(self.store.read_suite_report(batch_id))
        except FileNotFoundError:
            return build_suite_report(self.get_batch_summary(batch_id), self.store)

    # --- experiments (#155) ---

    def list_experiments(self) -> list[ExperimentSpec]:
        """Every experiment whose files load, in id order.

        An experiment whose plan or result does not load is left out here and
        named by :meth:`unreadable_experiments`, so one bad file cannot hide
        every other experiment.
        """
        specs = []
        for experiment_id in self.store.list_experiments():
            try:
                spec, _ = self.get_experiment(experiment_id)
            except (OSError, ValueError):
                continue
            specs.append(spec)
        return specs

    def unreadable_experiments(self) -> dict[str, str]:
        """Experiment id to the reason its plan or result does not load."""
        unreadable = {}
        for experiment_id in self.store.list_experiments():
            try:
                self.get_experiment(experiment_id)
            except (OSError, ValueError) as exc:
                unreadable[experiment_id] = str(exc)
        return unreadable

    def get_experiment(self, experiment_id: str) -> tuple[ExperimentSpec, ExperimentResult | None]:
        """The plan and, when a result has been recorded, what came back.

        Both files must name the experiment whose directory holds them, so a
        copied directory cannot pass as a second experiment.
        """
        spec = load_plan(self.store.read_experiment_spec(experiment_id))
        try:
            result = ExperimentResult.model_validate(
                self.store.read_experiment_result(experiment_id)
            )
        except FileNotFoundError:
            result = None
        named = {spec.experiment_id, experiment_id} | ({result.experiment_id} if result else set())
        if len(named) != 1:
            raise ValueError(
                f"{self.store.experiment_dir(experiment_id)} is {experiment_id!r} but its "
                f"files name {sorted(named - {experiment_id})}"
            )
        return spec, result

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
        """The three bundle artifacts covering this run, or None if it hasn't been bundled.

        The ``bundle`` stage writes the card after the other two, so a bundle
        cut short leaves no card and reads as not bundled. A card whose repair
        package or regression artifact is missing all the same, by hand or
        from an older writer, surfaces as a FileNotFoundError rather than a
        silently half-built bundle.

        A run that reproduced an earlier card (#211) holds ``bundle_ref.json``
        instead, and gets the bundle from the run it names. That card's
        ``run_id`` is the first occurrence and its ``occurrences`` include this
        run. A pointer to a run missing from this runs directory, as in a copy
        that left the first occurrence behind, raises FileNotFoundError naming
        both runs.
        """
        home = self._bundle_home(run_id)
        if home is None:
            return None
        return FailureBundle(
            failure_card=FailureCard.model_validate(self.store.read_json(home, names.FAILURE_CARD)),
            repair_package=RepairPackage.model_validate(
                self.store.read_json(home, names.REPAIR_PACKAGE)
            ),
            regression_artifact=RegressionArtifact.model_validate(
                self.store.read_json(home, names.REGRESSION_ARTIFACT)
            ),
        )

    def get_bundle_ref(self, run_id: str) -> BundleRef | None:
        """The pointer a reproduction holds in place of its own bundle (#211).

        None when the run holds its own bundle or was never bundled. The
        pointer's ``canonical_run_id`` names the run holding the card, as
        :meth:`ArtifactStore.bundle_home` resolves it.
        """
        ref = self._read_optional(run_id, names.BUNDLE_REF, BundleRef)
        return ref if isinstance(ref, BundleRef) else None

    def get_occurrences(self, run_id: str) -> list[BundleOccurrence] | None:
        """Every run the card covering this run lists, the first occurrence first.

        Works from any run on the card, its own run or a reproduction, and
        reads only the card. None when the run was never bundled. An empty
        list is a card written before failure card 0.5.0, which covers its own
        run alone.
        """
        home = self._bundle_home(run_id)
        if home is None:
            return None
        return FailureCard.model_validate(
            self.store.read_json(home, names.FAILURE_CARD)
        ).occurrences

    # --- internals ---

    def _bundle_home(self, run_id: str) -> str | None:
        self._require_run(run_id)
        home = self.store.bundle_home(run_id)
        if home is not None and not self.store.run_dir(home).is_dir():
            raise FileNotFoundError(
                f"run '{run_id}' reproduces the card in run '{home}', which is not in "
                f"{self.store.runs_dir}"
            )
        return home

    def _require_run(self, run_id: str) -> None:
        if not self.store.run_dir(run_id).is_dir():
            raise RunNotFound(f"run not found: '{run_id}' (looked in {self.store.run_dir(run_id)})")

    def _read_optional(self, run_id: str, name: str, model: type[BaseModel]) -> BaseModel | None:
        self._require_run(run_id)
        if not self.store.exists(run_id, name):
            return None
        return model.model_validate(self.store.read_json(run_id, name))
