"""Measure the size of the public results, for the Free plan sizing in docs.

    PYTHONPATH=src:tests python scripts/measure_public_results.py

Prints every figure in the "Free tier sizing" table of docs/public_results.md.
The rows are built the way the uploader builds them, from docs/acceptance/
through RunReader, and loaded into a throwaway PostgreSQL cluster
(tests/pg_cluster.py) through PostgREST's upsert statement as service_role,
using tests/postgrest_fake.py's PsqlPostgrest and the real uploader. Sizes are
read with pg_total_relation_size after vacuum analyze. The cluster listens on a
unix socket in a temp dir and is stopped and deleted before the script exits.

The sweep is modelled as two providers by five seeds by the 32 tasks of
refund_v0, 320 runs in two batches of 160, all hosted. Each sweep run row is a
copy of a retained run row, taking the retained rows largest first in turn,
with a fresh run id and batch id. Each sweep batch is the larger retained batch
with its entries and report rows repeated to 160.

A stored run row's share of its JSON is the sum of pg_column_size over the
row's columns, which counts a TOASTed value at its compressed size, over the
length of the row's JSON as the uploader sends it.
"""

from __future__ import annotations

import copy
import itertools
import json
import statistics
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ACCEPTANCE = REPO_ROOT / "docs" / "acceptance"
SWEEP_RUNS = 320
SWEEP_BATCHES = 2


def _json_bytes(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _tree_bytes(path: Path) -> tuple[int, int]:
    files = [p for p in path.rglob("*") if p.is_file()]
    return len(files), sum(p.stat().st_size for p in files)


def _sweep_rows(rows: dict[str, list[dict]]) -> dict[str, list[dict]]:
    from trace_harness.public_results import schema
    from trace_harness.public_results.rows import content_sha256

    per_batch = SWEEP_RUNS // SWEEP_BATCHES
    batch_ids = [f"batch_20260930T000000Z_sweep{n:03d}" for n in range(SWEEP_BATCHES)]
    largest_first = sorted(rows[schema.RUNS], key=_json_bytes, reverse=True)
    runs = []
    for n, source in zip(range(SWEEP_RUNS), itertools.cycle(largest_first), strict=False):
        run_id = f"run_20260930T000000Z_{n:08x}"
        batch_id = batch_ids[n // per_batch]
        row = copy.deepcopy(source)
        row.update(run_id=run_id, batch_id=batch_id, canonical_run_id=None)
        row["summary"].update(run_id=run_id, batch_id=batch_id)
        row["run_result"]["run_id"] = run_id
        row["content_sha256"] = content_sha256(schema.RUNS, row)
        runs.append(row)

    template = max(rows[schema.BATCHES], key=_json_bytes)
    batches = []
    for batch_id in batch_ids:
        row = copy.deepcopy(template)
        row["batch_id"] = batch_id
        for part, key in (("summary", "entries"), ("suite_report", "rows")):
            row[part]["batch_id"] = batch_id
            items = row[part][key]
            row[part][key] = [
                copy.deepcopy(item) for item, _ in zip(itertools.cycle(items), range(per_batch))
            ]
        row["content_sha256"] = content_sha256(schema.BATCHES, row)
        batches.append(row)
    return {schema.RUNS: runs, schema.BATCHES: batches, schema.EXPERIMENTS: []}


def _relation_bytes(cluster: object, database: str) -> int:
    from trace_harness.public_results import schema

    tables = (schema.SCHEMA_VERSIONS, *schema.PRIMARY_KEYS)
    vacuum = cluster.psql("vacuum analyze;", database=database)
    if not vacuum.ok:
        raise RuntimeError(vacuum.stderr)
    total = " + ".join(f"pg_total_relation_size('public.{t}')" for t in tables)
    done = cluster.psql(f"select {total};", database=database, tuples_only=True)
    if not done.ok:
        raise RuntimeError(done.stderr)
    return int(done.stdout.strip())


def _stored_run_bytes(cluster: object, database: str) -> dict[str, int]:
    from trace_harness.public_results import schema

    columns = " + ".join(f"coalesce(pg_column_size({c}), 0)" for c in schema.COLUMNS[schema.RUNS])
    done = cluster.psql(
        f"select json_object_agg(run_id, {columns}) from public.runs;",
        database=database,
        tuples_only=True,
    )
    if not done.ok:
        raise RuntimeError(done.stderr)
    return json.loads(done.stdout)


def _span(values: list[float], fmt: str = "{:,.0f}") -> str:
    return " / ".join(fmt.format(v) for v in (min(values), statistics.mean(values), max(values)))


def main() -> int:
    import postgrest_fake as fake
    from pg_cluster import PgCluster, find_postgres_bin
    from trace_harness.public_results import schema
    from trace_harness.public_results.postgrest import PostgrestClient
    from trace_harness.public_results.retained import stage_retained
    from trace_harness.public_results.rows import build_rows
    from trace_harness.public_results.upload import upload
    from trace_harness.run_reader import RunReader

    bin_dir = find_postgres_bin()
    if bin_dir is None:
        print("error: no PostgreSQL server binaries found", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="trace-measure-") as tmp:
        staged = stage_retained(ACCEPTANCE, Path(tmp) / "staged")
        reader = RunReader.from_runs_dir(staged.runs_dir)
        rows = build_rows(reader, sorted(staged.batches), staged.bundle_refs)
        run_dirs = [_tree_bytes(ACCEPTANCE / source)[1] for source in staged.runs.values()]
    files, tree = _tree_bytes(ACCEPTANCE)
    run_json = {row["run_id"]: _json_bytes(row) for row in rows[schema.RUNS]}
    table_json = {table: sum(map(_json_bytes, table_rows)) for table, table_rows in rows.items()}
    sweep = _sweep_rows(rows)
    sweep_json = {table: sum(map(_json_bytes, table_rows)) for table, table_rows in sweep.items()}
    summaries = [{"summary": row["summary"]} for row in rows[schema.RUNS] + sweep[schema.RUNS]]

    cluster = PgCluster(bin_dir)
    try:
        cluster.start()
        database = cluster.new_database()
        for migration in sorted((REPO_ROOT / schema.MIGRATIONS_DIR).glob("*.sql")):
            applied = cluster.apply_file(migration, database=database)
            if not applied.ok:
                raise RuntimeError(applied.stderr)
        client = PostgrestClient(
            fake.BASE_URL, fake.SERVICE_KEY, transport=fake.PsqlPostgrest(cluster, database)
        )
        upload(client, rows)
        retained_pg = _relation_bytes(cluster, database)
        stored = _stored_run_bytes(cluster, database)
        upload(client, {table: rows[table] + sweep[table] for table in rows})
        with_sweep_pg = _relation_bytes(cluster, database)
        version = ".".join(map(str, cluster.version))
    finally:
        cluster.stop()

    shares = [100 * stored[run_id] / size for run_id, size in run_json.items()]
    runs, batches, experiments = (len(rows[t]) for t in schema.PRIMARY_KEYS)
    lines = [
        ("`docs/acceptance/`, " + f"{files} files", f"{tree:,} bytes"),
        (
            f"One retained run directory on disk, min / mean / max over {runs}",
            f"{_span(run_dirs)} bytes",
        ),
        ("One hosted run row as JSON, min / mean / max", f"{_span(list(run_json.values()))} bytes"),
        (
            f"Hosted rows as JSON, {runs} runs + {batches} batches + {experiments} "
            f"experiment{'' if experiments == 1 else 's'}",
            " + ".join(f"{table_json[t]:,}" for t in schema.PRIMARY_KEYS) + " bytes",
        ),
        (
            "Postgres size of the retained set, tables with TOAST and indexes",
            f"{retained_pg:,} bytes",
        ),
        (
            "A stored run row as a share of its JSON, min / mean / max",
            _span(shares, "{:.1f}") + " percent",
        ),
        (
            f"One sweep as JSON, {SWEEP_RUNS} run rows copied from the retained rows largest "
            f"first, plus two {SWEEP_RUNS // SWEEP_BATCHES}-entry batches",
            f"{sweep_json[schema.RUNS]:,} + {sweep_json[schema.BATCHES]:,} bytes",
        ),
        (
            "Postgres size of the retained set plus that sweep",
            f"{with_sweep_pg:,} bytes ({with_sweep_pg / 2**20:.1f} MiB)",
        ),
        (
            f"`list_runs()` response for all {len(summaries)} runs",
            f"{_json_bytes(summaries):,} bytes",
        ),
    ]
    print(f"Measured with PostgreSQL {version}.\n")
    print("| Quantity | Measured |\n|---|---|")
    for quantity, measured in lines:
        print(f"| {quantity} | {measured} |")
    egress = 5 * 10**9
    print(
        f"\nOne sweep adds {(with_sweep_pg - retained_pg) / 1e6:.1f} MB. 5 GB of egress is "
        f"{egress / statistics.mean(run_json.values()):,.0f} run reads at the mean row size, or "
        f"{egress / _json_bytes(summaries):,.0f} loads of the whole run list."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
