"""SupabaseRunReader: RunReader's reads, served from the hosted public results.

Same method names, parameters, return types and missing-artifact states as
:class:`trace_harness.run_reader.RunReader`, which stays the default and is not
changed. Each method reads one row, or one column of one row, from the tables
in ``supabase/migrations/`` through PostgREST, and validates the stored JSON
with the same model the filesystem reader uses. A test compares the two
backends method by method over every retained run, batch and experiment.

    list_runs()                -> runs.summary, every row, ordered by run_id
    list_runs_for_batch(id)    -> runs.summary where batch_id = id
    get_run / get_task / get_trace / get_verifier / get_attribution
                               -> the matching runs column
    get_bundle(id)             -> runs.failure_card, repair_package, regression_artifact,
                                  from the row canonical_run_id names when it is set
    get_batch_summary(id)      -> batches.summary
    get_suite_report(id)       -> batches.suite_report
    list_experiments()         -> experiments.spec, ordered by experiment_id, the ones that load
    unreadable_experiments()   -> the experiment ids whose spec or result does not load, and why
    get_experiment(id)         -> experiments.spec and experiments.result

Missing states match the filesystem reader. An unknown run id raises
RunNotFound. An artifact the run has not produced yet returns None. An unknown
batch or experiment raises FileNotFoundError. A retained run always has its
run result, task and trace, because the uploader only hosts runs that
``list_runs`` returns and the table requires those three columns.

It reads with the anonymous key, which row level security limits to reads, and
refuses a key that maps to service_role so the key that bypasses row level
security never ends up configured on a reader. Select it with
``TRACE_RUN_READER=supabase`` (see ``trace_harness.run_readers``).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from trace_harness.attribution.schemas import AttributionResult
from trace_harness.failure_bundles.generator import FailureBundle
from trace_harness.failure_bundles.schemas import FailureCard, RepairPackage
from trace_harness.public_results import schema
from trace_harness.public_results.postgrest import (
    DEFAULT_PAGE_SIZE,
    PostgrestClient,
    Transport,
    urllib_transport,
)
from trace_harness.regression.schemas import RegressionArtifact
from trace_harness.run_reader import RunNotFound, RunSummary
from trace_harness.runner.batch import BatchSummary
from trace_harness.runner.experiment import ExperimentResult, ExperimentSpec, load_plan
from trace_harness.runner.report import SuiteReport
from trace_harness.runner.result import RunResult
from trace_harness.tasks.schemas import TaskSpec
from trace_harness.tracing.events import TraceEvent
from trace_harness.verifiers.base import VerifierResult

URL_ENV = "TRACE_SUPABASE_URL"
ANON_KEY_ENV = "TRACE_SUPABASE_ANON_KEY"
_BUNDLE_COLUMNS = "failure_card,repair_package,regression_artifact"


class SupabaseRunReader:
    """Typed reads over the hosted public results tables."""

    def __init__(self, client: PostgrestClient):
        if client.role == "service_role":
            raise ValueError(
                "SupabaseRunReader was given a key for service_role, which bypasses row level "
                f"security. Readers use the anonymous key ({ANON_KEY_ENV})."
            )
        self.client = client

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        transport: Transport = urllib_transport,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> SupabaseRunReader:
        """Build a reader from ``TRACE_SUPABASE_URL`` and ``TRACE_SUPABASE_ANON_KEY``."""
        env = os.environ if env is None else env
        url, key = env.get(URL_ENV, ""), env.get(ANON_KEY_ENV, "")
        missing = [name for name, value in ((URL_ENV, url), (ANON_KEY_ENV, key)) if not value]
        if missing:
            raise ValueError(
                f"the supabase run reader needs {' and '.join(missing)} "
                "(see docs/public_results.md)"
            )
        return cls(PostgrestClient(url, key, transport=transport, page_size=page_size))

    @property
    def location(self) -> str:
        return self.client.base_url

    # --- listing ---

    def list_runs(self) -> list[RunSummary]:
        """Summaries of every hosted run, oldest-first (chronological)."""
        rows = self.client.select(schema.RUNS, "summary", order="run_id.asc")
        return [RunSummary.model_validate(row["summary"]) for row in rows]

    def list_runs_for_batch(self, batch_id: str) -> list[RunSummary]:
        """All runs tagged with ``batch_id``, oldest-first (chronological)."""
        rows = self.client.select(
            schema.RUNS, "summary", filters={"batch_id": f"eq.{batch_id}"}, order="run_id.asc"
        )
        return [RunSummary.model_validate(row["summary"]) for row in rows]

    # --- batches ---

    def get_batch_summary(self, batch_id: str) -> BatchSummary:
        """The authoritative summary for one batch (raises if the batch is unknown)."""
        return BatchSummary.model_validate(self._batch_column(batch_id, "summary"))

    def get_suite_report(self, batch_id: str) -> SuiteReport:
        """The suite report the uploader read through RunReader for this batch."""
        return SuiteReport.model_validate(self._batch_column(batch_id, "suite_report"))

    # --- experiments (#155) ---

    def list_experiments(self) -> list[ExperimentSpec]:
        """Every hosted experiment whose plan and result load, oldest first by id.

        One that does not load is left out here and named by
        :meth:`unreadable_experiments`, as the filesystem reader does, so one
        bad row cannot hide every other experiment.
        """
        loaded, _ = self._experiments()
        return loaded

    def unreadable_experiments(self) -> dict[str, str]:
        """Experiment id to the reason its hosted plan or result does not load."""
        _, unreadable = self._experiments()
        return unreadable

    def get_experiment(self, experiment_id: str) -> tuple[ExperimentSpec, ExperimentResult | None]:
        """The plan and, when a result has been recorded, what came back.

        The plan loads through ``load_plan``, as the filesystem reader loads
        ``experiment.json``, so a plan that file would refuse is refused here.
        """
        row = self.client.select_one(
            schema.EXPERIMENTS, "spec,result", "experiment_id", experiment_id
        )
        if row is None:
            raise FileNotFoundError(
                f"experiment.json not found for experiment '{experiment_id}' "
                f"(looked in {self._table_url(schema.EXPERIMENTS)})."
            )
        return self._load_experiment(experiment_id, row)

    # --- single run ---

    def get_run(self, run_id: str) -> RunResult:
        return RunResult.model_validate(self._run_columns(run_id, "run_result")["run_result"])

    def get_task(self, run_id: str) -> TaskSpec:
        return TaskSpec.model_validate(self._run_columns(run_id, "task_spec")["task_spec"])

    def get_trace(self, run_id: str) -> list[TraceEvent]:
        events = self._run_columns(run_id, "trace")["trace"]
        return [TraceEvent.model_validate(event) for event in events]

    def get_verifier(self, run_id: str) -> VerifierResult | None:
        return self._optional(run_id, "verifier_result", VerifierResult)

    def get_attribution(self, run_id: str) -> AttributionResult | None:
        return self._optional(run_id, "attribution_result", AttributionResult)

    def get_bundle(self, run_id: str) -> FailureBundle | None:
        """The three bundle artifacts covering this run, or None if it hasn't been bundled.

        A run that reproduced an earlier card (#211) has its row's
        ``canonical_run_id`` set, and gets the bundle from that run's row, the
        way the filesystem reader follows ``bundle_ref.json``.
        """
        row = self._run_columns(run_id, f"{_BUNDLE_COLUMNS},canonical_run_id")
        canonical = row.get("canonical_run_id")
        if canonical is not None:
            home = self.client.select_one(schema.RUNS, _BUNDLE_COLUMNS, "run_id", canonical)
            if home is None or home["failure_card"] is None:
                raise FileNotFoundError(
                    f"run '{run_id}' is covered by the failure card of run '{canonical}', "
                    f"which {self._table_url(schema.RUNS)} does not hold"
                )
            row = home
        parts = (row["failure_card"], row["repair_package"], row["regression_artifact"])
        if all(part is None for part in parts):
            return None
        if any(part is None for part in parts):
            raise FileNotFoundError(f"partial bundle for run '{run_id}' in the hosted results")
        return FailureBundle(
            failure_card=FailureCard.model_validate(parts[0]),
            repair_package=RepairPackage.model_validate(parts[1]),
            regression_artifact=RegressionArtifact.model_validate(parts[2]),
        )

    # --- internals ---

    def _experiments(self) -> tuple[list[ExperimentSpec], dict[str, str]]:
        rows = self.client.select(
            schema.EXPERIMENTS, "experiment_id,spec,result", order="experiment_id.asc"
        )
        loaded, unreadable = [], {}
        for row in rows:
            try:
                spec, _ = self._load_experiment(row["experiment_id"], row)
            except ValueError as exc:
                unreadable[row["experiment_id"]] = str(exc)
                continue
            loaded.append(spec)
        return loaded, unreadable

    def _load_experiment(
        self, experiment_id: str, row: Mapping[str, Any]
    ) -> tuple[ExperimentSpec, ExperimentResult | None]:
        spec = load_plan(row["spec"])
        stored = row.get("result")
        result = None if stored is None else ExperimentResult.model_validate(stored)
        named = {spec.experiment_id, experiment_id} | ({result.experiment_id} if result else set())
        if len(named) != 1:
            raise ValueError(
                f"hosted experiment {experiment_id!r} holds a plan or result naming "
                f"{sorted(named - {experiment_id})}"
            )
        return spec, result

    def _table_url(self, table: str) -> str:
        return f"{self.client.base_url}/rest/v1/{table}"

    def _run_columns(self, run_id: str, columns: str) -> dict[str, Any]:
        row = self.client.select_one(schema.RUNS, columns, "run_id", run_id)
        if row is None:
            raise RunNotFound(
                f"run not found: '{run_id}' (looked in {self._table_url(schema.RUNS)})"
            )
        return row

    def _batch_column(self, batch_id: str, column: str) -> Any:
        row = self.client.select_one(schema.BATCHES, column, "batch_id", batch_id)
        if row is None:
            raise FileNotFoundError(
                f"batch summary not found for batch '{batch_id}' "
                f"(looked in {self._table_url(schema.BATCHES)})."
            )
        return row[column]

    def _optional(self, run_id: str, column: str, model: type[BaseModel]) -> Any:
        value = self._run_columns(run_id, column)[column]
        return None if value is None else model.model_validate(value)
