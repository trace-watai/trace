"""Turn what RunReader returns into rows for the public results tables.

Every jsonb column holds ``model_dump(mode="json")`` of the model a RunReader
method returned, which is the same serialization ``ArtifactStore.write_json``
uses, so a hosted artifact is the JSON the pipeline writes, ``schema_version``
included. Rows are built only from RunReader calls. Nothing here opens an
artifact file or the run index.

The one input from outside RunReader is ``bundle_refs``, which runs are
reproductions of an earlier failure card (#211) and which run holds that card,
as the stager read it from each ``bundle_ref.json``. A reproduction's row keeps
the three bundle columns null and names the card's run in
``canonical_run_id``. Copying the card into every reproduction's row would
repeat it once per occurrence and change all of those rows whenever the card
gains an occurrence. ``get_bundle`` is never called for such a run, because
since #211 it returns the card of the run the pointer names.

``content_sha256`` is a digest of the row, used by the uploader to skip rows
the project already holds. It covers every column except itself and the fields
in ``schema.VOLATILE_FIELDS``, plus ``RESULTS_SCHEMA_VERSION``, so a new SQL
schema version re-uploads everything once.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, Protocol

from pydantic import BaseModel

from trace_harness.public_results import schema

Row = dict[str, Any]


class RetainedReader(Protocol):
    """The RunReader methods rows are built from (both backends satisfy it)."""

    def list_runs(self) -> list[Any]: ...
    def get_run(self, run_id: str) -> Any: ...
    def get_task(self, run_id: str) -> Any: ...
    def get_trace(self, run_id: str) -> list[Any]: ...
    def get_verifier(self, run_id: str) -> Any: ...
    def get_attribution(self, run_id: str) -> Any: ...
    def get_bundle(self, run_id: str) -> Any: ...
    def get_batch_summary(self, batch_id: str) -> Any: ...
    def get_suite_report(self, batch_id: str) -> Any: ...
    def list_experiments(self) -> list[Any]: ...
    def get_experiment(self, experiment_id: str) -> tuple[Any, Any]: ...


def _dump(model: BaseModel | None) -> Any:
    return None if model is None else model.model_dump(mode="json")


def content_sha256(table: str, row: Row) -> str:
    """The digest of ``row`` that decides whether an upload would change anything."""
    hashed = {k: v for k, v in row.items() if k != schema.CONTENT_SHA256}
    for column, key in schema.VOLATILE_FIELDS.get(table, ()):
        value = hashed.get(column)
        if isinstance(value, dict) and key in value:
            hashed[column] = {k: v for k, v in value.items() if k != key}
    canonical = json.dumps(
        {"results_schema": schema.RESULTS_SCHEMA_VERSION, "table": table, "row": hashed},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _finish(table: str, row: Row) -> Row:
    row[schema.CONTENT_SHA256] = content_sha256(table, row)
    if tuple(row) != schema.COLUMNS[table]:
        raise RuntimeError(f"{table} row columns drifted from schema.COLUMNS")
    return row


def run_rows(reader: RetainedReader, bundle_refs: Mapping[str, str] | None = None) -> list[Row]:
    """One row per run RunReader lists, oldest first.

    ``bundle_refs`` maps a reproduction's run id to the run holding its card.
    """
    bundle_refs = bundle_refs or {}
    rows = []
    for summary in reader.list_runs():
        run_id = summary.run_id
        canonical_run_id = bundle_refs.get(run_id)
        bundle = None if canonical_run_id is not None else reader.get_bundle(run_id)
        row: Row = {
            "run_id": run_id,
            "task_id": summary.task_id,
            "batch_id": summary.batch_id,
            "summary": _dump(summary),
            "run_result": _dump(reader.get_run(run_id)),
            "task_spec": _dump(reader.get_task(run_id)),
            "trace": [_dump(event) for event in reader.get_trace(run_id)],
            "verifier_result": _dump(reader.get_verifier(run_id)),
            "attribution_result": _dump(reader.get_attribution(run_id)),
            "failure_card": _dump(bundle.failure_card) if bundle else None,
            "repair_package": _dump(bundle.repair_package) if bundle else None,
            "regression_artifact": _dump(bundle.regression_artifact) if bundle else None,
            "canonical_run_id": canonical_run_id,
        }
        rows.append(_finish(schema.RUNS, row))
    return rows


def batch_rows(reader: RetainedReader, batch_ids: list[str]) -> list[Row]:
    """One row per batch id. RunReader has no batch listing, so ids are passed in."""
    rows = []
    for batch_id in sorted(batch_ids):
        row: Row = {
            "batch_id": batch_id,
            "summary": _dump(reader.get_batch_summary(batch_id)),
            "suite_report": _dump(reader.get_suite_report(batch_id)),
        }
        rows.append(_finish(schema.BATCHES, row))
    return rows


def experiment_rows(reader: RetainedReader) -> list[Row]:
    """One row per experiment RunReader lists, oldest first by id."""
    rows = []
    for listed in reader.list_experiments():
        spec, result = reader.get_experiment(listed.experiment_id)
        row: Row = {
            "experiment_id": spec.experiment_id,
            "spec": _dump(spec),
            "result": _dump(result),
        }
        rows.append(_finish(schema.EXPERIMENTS, row))
    return rows


def build_rows(
    reader: RetainedReader,
    batch_ids: list[str],
    bundle_refs: Mapping[str, str] | None = None,
) -> dict[str, list[Row]]:
    """Every row for every table, keyed by table name in write order."""
    return {
        schema.RUNS: run_rows(reader, bundle_refs),
        schema.BATCHES: batch_rows(reader, batch_ids),
        schema.EXPERIMENTS: experiment_rows(reader),
    }
