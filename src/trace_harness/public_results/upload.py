"""Push the retained evidence to the hosted public results, idempotently.

    python -m trace_harness.public_results.upload [docs/acceptance] [--prune]
        [--dry-run] [--offline [--dump DIR]]

The retained tree is staged into a temp dir (``retained.stage_retained``) and
read through RunReader. Rows are built from what RunReader returns
(``rows.build_rows``), never from the files or the run index directly.

Idempotence. The uploader first reads each table's natural keys and content
hashes, then upserts only the rows that are missing or whose hash differs, on
the natural key with ``resolution=merge-duplicates``. A rerun over the same
tree therefore sends no write at all. If it did send one, the upsert would
replace a row with an identical row. ``--prune`` deletes hosted rows that are
no longer retained, so the hosted set follows main when evidence is removed.
It refuses to run over an empty retained set, so a staging mistake can never
wipe the project.

Configuration comes from ``TRACE_SUPABASE_URL`` and
``TRACE_SUPABASE_SERVICE_KEY``, repository secrets in CI. The service key maps
to service_role, which bypasses row level security, and it is the only key
that can write. The uploader refuses a key it can tell is the anonymous one.
Before writing it checks that the project has the SQL schema version this code
expects, so a migration that was never applied fails loudly instead of half
writing.

Exit codes: 0 done (or nothing to do), 1 the project refused or is on the wrong
schema, 2 a configuration or input problem.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from trace_harness.public_results import schema
from trace_harness.public_results.postgrest import (
    PostgrestClient,
    PostgrestError,
    Transport,
    urllib_transport,
)
from trace_harness.public_results.retained import stage_retained
from trace_harness.public_results.rows import Row, build_rows
from trace_harness.run_reader import RunReader
from trace_harness.run_reader_supabase import URL_ENV

SERVICE_KEY_ENV = "TRACE_SUPABASE_SERVICE_KEY"
DEFAULT_RETAINED = "docs/acceptance"
# Keep each POST well under any gateway body limit. One retained run row is
# 60 to 150 KB, so a request carries a handful of runs.
MAX_REQUEST_BYTES = 1_000_000
DELETE_CHUNK = 100


class UploadError(RuntimeError):
    """The upload cannot go ahead safely."""


@dataclass
class TablePlan:
    table: str
    retained: int
    inserts: list[Row] = field(default_factory=list)
    updates: list[Row] = field(default_factory=list)
    unchanged: int = 0
    orphans: list[str] = field(default_factory=list)

    @property
    def upserts(self) -> list[Row]:
        return self.inserts + self.updates


def prepare_rows(retained_root: Path | str, staging_dir: Path | str) -> dict[str, list[Row]]:
    """Stage the retained tree and build every row through RunReader."""
    staged = stage_retained(retained_root, staging_dir)
    reader = RunReader.from_runs_dir(staged.runs_dir)
    return build_rows(reader, sorted(staged.batches), staged.bundle_refs)


def check_schema(client: PostgrestClient) -> None:
    rows = client.select(
        schema.SCHEMA_VERSIONS,
        "version",
        filters={"version": f"eq.{schema.RESULTS_SCHEMA_VERSION}"},
    )
    if not rows:
        raise UploadError(
            f"the project does not have public results SQL schema "
            f"{schema.RESULTS_SCHEMA_VERSION}. Apply the migrations in "
            f"{schema.MIGRATIONS_DIR}/ first (docs/public_results.md)."
        )


def remote_hashes(client: PostgrestClient, table: str) -> dict[str, str]:
    key = schema.PRIMARY_KEYS[table]
    rows = client.select(table, f"{key},{schema.CONTENT_SHA256}", order=f"{key}.asc")
    return {row[key]: row[schema.CONTENT_SHA256] for row in rows}


def plan_table(table: str, rows: list[Row], remote: Mapping[str, str]) -> TablePlan:
    key = schema.PRIMARY_KEYS[table]
    plan = TablePlan(table, retained=len(rows))
    local_keys = set()
    for row in rows:
        local_keys.add(row[key])
        known = remote.get(row[key])
        if known is None:
            plan.inserts.append(row)
        elif known != row[schema.CONTENT_SHA256]:
            plan.updates.append(row)
        else:
            plan.unchanged += 1
    plan.orphans = sorted(k for k in remote if k not in local_keys)
    return plan


def request_chunks(rows: list[Row], max_bytes: int = MAX_REQUEST_BYTES) -> list[list[Row]]:
    """Group rows into POST bodies under ``max_bytes``. A larger row goes alone."""
    chunks: list[list[Row]] = []
    current: list[Row] = []
    size = 2
    for row in rows:
        row_size = len(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode()) + 1
        if current and size + row_size > max_bytes:
            chunks.append(current)
            current, size = [], 2
        current.append(row)
        size += row_size
    if current:
        chunks.append(current)
    return chunks


def upload(
    client: PostgrestClient,
    rows_by_table: Mapping[str, list[Row]],
    *,
    prune: bool = False,
    dry_run: bool = False,
    max_request_bytes: int = MAX_REQUEST_BYTES,
) -> list[TablePlan]:
    """Bring the hosted tables in line with ``rows_by_table``. Returns what it did."""
    if prune and not any(rows_by_table.values()):
        raise UploadError("refusing to prune: the retained set is empty")
    check_schema(client)
    plans = [
        plan_table(table, rows, remote_hashes(client, table))
        for table, rows in rows_by_table.items()
    ]
    if dry_run:
        return plans
    for plan in plans:
        key = schema.PRIMARY_KEYS[plan.table]
        for chunk in request_chunks(plan.upserts, max_request_bytes):
            client.upsert(plan.table, chunk, on_conflict=key)
    if prune:
        for plan in plans:
            key = schema.PRIMARY_KEYS[plan.table]
            for start in range(0, len(plan.orphans), DELETE_CHUNK):
                client.delete(plan.table, key, plan.orphans[start : start + DELETE_CHUNK])
    return plans


def _row_bytes(rows: list[Row]) -> int:
    return sum(len(json.dumps(r, ensure_ascii=False, separators=(",", ":")).encode()) for r in rows)


def _describe(plans: list[TablePlan], *, prune: bool, dry_run: bool) -> list[str]:
    lines = []
    for plan in plans:
        orphan_word = "pruned" if prune and not dry_run else "not retained, left in place"
        if prune and dry_run:
            orphan_word = "would be pruned"
        lines.append(
            f"{plan.table}: {plan.retained} retained, {len(plan.inserts)} new, "
            f"{len(plan.updates)} changed, {plan.unchanged} unchanged, "
            f"{len(plan.orphans)} {orphan_word}"
        )
        if plan.orphans and not prune:
            lines.append(f"  hosted but not retained: {', '.join(plan.orphans)}")
    return lines


def main(
    argv: list[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    transport: Transport = urllib_transport,
) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m trace_harness.public_results.upload",
        description="Upload the retained runs, batches and experiments to the public results.",
    )
    parser.add_argument("retained", nargs="?", default=DEFAULT_RETAINED)
    parser.add_argument("--prune", action="store_true", help="delete hosted rows not retained")
    parser.add_argument("--dry-run", action="store_true", help="plan against the project only")
    parser.add_argument(
        "--offline", action="store_true", help="build the rows and report sizes, no network"
    )
    parser.add_argument("--dump", metavar="DIR", help="with --offline, write the rows as JSON")
    args = parser.parse_args(argv)
    env = os.environ if env is None else env

    try:
        with tempfile.TemporaryDirectory(prefix="trace-public-results-") as tmp:
            rows_by_table = prepare_rows(args.retained, Path(tmp) / "staged")
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for table, rows in rows_by_table.items():
        print(f"{table}: {len(rows)} rows, {_row_bytes(rows)} bytes of JSON")

    if args.offline:
        if args.dump:
            out = Path(args.dump)
            out.mkdir(parents=True, exist_ok=True)
            for table, rows in rows_by_table.items():
                (out / f"{table}.json").write_text(
                    json.dumps(rows, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
                )
            print(f"rows written to {out}")
        return 0
    if args.dump:
        print("error: --dump needs --offline", file=sys.stderr)
        return 2

    url, key = env.get(URL_ENV, ""), env.get(SERVICE_KEY_ENV, "")
    missing = [name for name, value in ((URL_ENV, url), (SERVICE_KEY_ENV, key)) if not value]
    if missing:
        print(f"error: {' and '.join(missing)} must be set", file=sys.stderr)
        return 2
    try:
        client = PostgrestClient(url, key, transport=transport)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if client.role == "anon":
        print(
            f"error: {SERVICE_KEY_ENV} holds the anonymous key, which cannot write. "
            "Use the service key.",
            file=sys.stderr,
        )
        return 2

    try:
        plans = upload(client, rows_by_table, prune=args.prune, dry_run=args.dry_run)
    except (PostgrestError, UploadError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for line in _describe(plans, prune=args.prune, dry_run=args.dry_run):
        print(line)
    written = sum(len(p.upserts) for p in plans)
    if args.dry_run:
        print(f"dry run: {written} row(s) would be written to {client.base_url}")
    elif written or (args.prune and any(p.orphans for p in plans)):
        print(f"published to {client.base_url}")
    else:
        print(f"nothing to publish, {client.base_url} already matches")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
