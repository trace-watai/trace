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
    get_bundle(id)             -> runs.failure_card, repair_package, regression_artifact
    get_batch_summary(id)      -> batches.summary
    get_suite_report(id)       -> batches.suite_report
    list_experiments()         -> experiments.spec, ordered by experiment_id
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
from trace_harness.runner.experiment import ExperimentResult, ExperimentSpec
from trace_harness.runner.report import SuiteReport
from trace_harness.runner.result import RunResult
from trace_harness.tasks.schemas import TaskSpec
from trace_harness.tracing.events import TraceEvent
from trace_harness.verifiers.base import VerifierResult

URL_ENV = "TRACE_SUPABASE_URL"
ANON_KEY_ENV = "TRACE_SUPABASE_ANON_KEY"


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
        """Every hosted experiment plan, oldest first by id."""
        rows = self.client.select(schema.EXPERIMENTS, "spec", order="experiment_id.asc")
        return [ExperimentSpec.model_validate(row["spec"]) for row in rows]

    def get_experiment(self, experiment_id: str) -> tuple[ExperimentSpec, ExperimentResult | None]:
        """The plan and, when a result has been recorded, what came back."""
        row = self.client.select_one(
            schema.EXPERIMENTS, "spec,result", "experiment_id", experiment_id
        )
        if row is None:
            raise FileNotFoundError(
                f"experiment.json not found for experiment '{experiment_id}' "
                f"(looked in {self._table_url(schema.EXPERIMENTS)})."
            )
        result = row.get("result")
        return (
            ExperimentSpec.model_validate(row["spec"]),
            None if result is None else ExperimentResult.model_validate(result),
        )

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
        """The three bundle artifacts, or None if the run hasn't been bundled."""
        row = self._run_columns(run_id, "failure_card,repair_package,regression_artifact")
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
