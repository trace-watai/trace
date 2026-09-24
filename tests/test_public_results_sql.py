"""The public results SQL schema, applied to a real Postgres.

Every migration under ``supabase/migrations/`` is applied to a fresh database
that first gets an approximation of Supabase's role bootstrap (see
``pg_cluster.py``). The tests then act as each API role the way PostgREST does,
with ``set role``, and check the done-when from #205 directly. Anonymous reads
see every row, and an anonymous write is refused. The refusal is proved twice.
Once as shipped, where anon holds no write privilege. Once with write privileges
granted back by hand, where only row level security stands in the way.

Skips when no PostgreSQL server binaries are installed. The lockstep test at the
bottom needs no database and always runs.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

import postgrest_fake as fake
from pg_cluster import SUPABASE_BOOTSTRAP, PgCluster, dollar_quote, find_postgres_bin
from trace_harness.public_results import schema
from trace_harness.public_results.postgrest import PostgrestClient, PostgrestError
from trace_harness.public_results.upload import upload
from trace_harness.run_reader_supabase import SupabaseRunReader

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = sorted((REPO_ROOT / schema.MIGRATIONS_DIR).glob("*.sql"))
WRITABLE = tuple(schema.PRIMARY_KEYS)
ALL_TABLES = (schema.SCHEMA_VERSIONS, *WRITABLE)


@pytest.fixture(scope="module")
def cluster() -> Iterator[PgCluster]:
    bin_dir = find_postgres_bin()
    if bin_dir is None:
        pytest.skip("no PostgreSQL server binaries found (initdb, pg_ctl, psql, postgres)")
    pg = PgCluster(bin_dir)
    try:
        pg.start()
        yield pg
    finally:
        pg.stop()


@pytest.fixture(scope="module")
def migrated(cluster: PgCluster) -> str:
    """A database with every migration applied, used as a template."""
    name = cluster.new_database()
    for migration in MIGRATIONS:
        applied = cluster.apply_file(migration, database=name)
        assert applied.ok, f"{migration.name}: {applied.stderr}"
    return name


@pytest.fixture
def db(cluster: PgCluster, migrated: str) -> str:
    """A fresh migrated database for a test that changes rows or grants."""
    return cluster.copy_database(migrated)


@pytest.fixture(scope="module")
def loaded(cluster: PgCluster, migrated: str) -> str:
    """One migrated database holding the sample rows, shared by tests that only read."""
    name = cluster.copy_database(migrated)
    load_sample(cluster, name)
    return name


def upsert_sql(table: str, rows: list[dict]) -> str:
    """The statement PostgREST runs for a POST with resolution=merge-duplicates.

    PostgREST reads the JSON body with json_populate_recordset and resolves
    conflicts on the on_conflict columns, updating every column the body names.
    """
    columns = ", ".join(schema.COLUMNS[table])
    key = schema.PRIMARY_KEYS[table]
    updates = ", ".join(f"{c} = excluded.{c}" for c in schema.COLUMNS[table] if c != key)
    body = dollar_quote(json.dumps(rows))
    return (
        f"insert into public.{table} ({columns}) select {columns} "
        f"from jsonb_populate_recordset(null::public.{table}, {body}::jsonb) "
        f"on conflict ({key}) do update set {updates};"
    )


def sample_rows() -> dict[str, list[dict]]:
    sha = "0" * 64
    return {
        schema.RUNS: [
            {
                "run_id": "run_a",
                "task_id": "task_a",
                "batch_id": "batch_a",
                "summary": {"run_id": "run_a", "task_id": "task_a", "batch_id": "batch_a"},
                "run_result": {"run_id": "run_a"},
                "task_spec": {"task_id": "task_a"},
                "trace": [{"step": 0}],
                "verifier_result": {"passed": False},
                "attribution_result": None,
                "failure_card": None,
                "repair_package": None,
                "regression_artifact": None,
                "content_sha256": sha,
            }
        ],
        schema.BATCHES: [
            {
                "batch_id": "batch_a",
                "summary": {"batch_id": "batch_a"},
                "suite_report": {"batch_id": "batch_a"},
                "content_sha256": sha,
            }
        ],
        schema.EXPERIMENTS: [
            {
                "experiment_id": "exp_a",
                "spec": {"experiment_id": "exp_a"},
                "result": None,
                "content_sha256": sha,
            }
        ],
    }


def load_sample(cluster: PgCluster, db: str) -> None:
    for table, rows in sample_rows().items():
        done = cluster.psql(f"set role service_role;\n{upsert_sql(table, rows)}", database=db)
        assert done.ok, done.stderr


def scalar(cluster: PgCluster, db: str, sql: str, role: str = "postgres") -> str:
    done = cluster.psql(f"set role {role};\n{sql}", database=db, tuples_only=True)
    assert done.ok, done.stderr
    return done.stdout.strip()


def write_statements(table: str) -> dict[str, str]:
    key = schema.PRIMARY_KEYS[table]
    row = sample_rows()[table][0] | {key: f"{table}_new"}
    if table == schema.RUNS:
        row["summary"] = row["summary"] | {"run_id": "runs_new"}
        row["run_result"] = {"run_id": "runs_new"}
    else:
        field = "summary" if table == schema.BATCHES else "spec"
        row[field] = {key: row[key]}
        if table == schema.BATCHES:
            row["suite_report"] = {key: row[key]}
    return {
        "insert": upsert_sql(table, [row]).split(" on conflict")[0] + ";",
        "update": f"update public.{table} set content_sha256 = '{'1' * 64}';",
        "delete": f"delete from public.{table};",
        "truncate": f"truncate public.{table};",
    }


def test_migrations_apply_and_record_the_python_version(cluster: PgCluster, migrated: str) -> None:
    versions = scalar(
        cluster, migrated, "select string_agg(version, ',') from public.schema_versions"
    )
    assert versions.split(",")[-1] == schema.RESULTS_SCHEMA_VERSION


def test_every_table_in_public_has_row_level_security(cluster: PgCluster, migrated: str) -> None:
    missing = scalar(
        cluster,
        migrated,
        "select coalesce(string_agg(c.relname, ','), '') from pg_class c "
        "join pg_namespace n on n.oid = c.relnamespace "
        "where n.nspname = 'public' and c.relkind = 'r' and not c.relrowsecurity",
    )
    assert missing == ""
    tables = scalar(
        cluster,
        migrated,
        "select string_agg(tablename, ',' order by tablename) from pg_tables "
        "where schemaname = 'public'",
    )
    assert sorted(tables.split(",")) == sorted(ALL_TABLES)


def table_md5(cluster: PgCluster, db: str, table: str, role: str = "postgres") -> str:
    """A digest of every row in ``table`` that ``role`` can see."""
    rows = f"select coalesce(string_agg(t::text, '' order by t::text), '') from public.{table} t"
    return scalar(cluster, db, f"select md5(({rows}))", role)


@pytest.mark.parametrize("role", ["anon", "authenticated"])
def test_readers_see_every_row(cluster: PgCluster, loaded: str, role: str) -> None:
    for table in ALL_TABLES:
        expected = scalar(cluster, loaded, f"select count(*) from public.{table}")
        assert int(expected) >= 1
        assert scalar(cluster, loaded, f"select count(*) from public.{table}", role) == expected
        assert table_md5(cluster, loaded, table, role) == table_md5(cluster, loaded, table)


@pytest.mark.parametrize("role", ["anon", "authenticated"])
@pytest.mark.parametrize("table", ALL_TABLES)
def test_reader_writes_are_refused(cluster: PgCluster, loaded: str, role: str, table: str) -> None:
    if table == schema.SCHEMA_VERSIONS:
        statements = {
            "insert": "insert into public.schema_versions values ('9', 'x');",
            "update": "update public.schema_versions set description = 'x';",
            "delete": "delete from public.schema_versions;",
            "truncate": "truncate public.schema_versions;",
        }
    else:
        statements = write_statements(table)
    before = table_md5(cluster, loaded, table)
    for operation, statement in statements.items():
        done = cluster.psql(f"set role {role};\n{statement}", database=loaded)
        assert not done.ok, f"{role} {operation} on {table} succeeded"
        assert "permission denied" in done.stderr, f"{operation}: {done.stderr}"
    assert table_md5(cluster, loaded, table) == before


@pytest.mark.parametrize("table", WRITABLE)
def test_row_level_security_refuses_anon_writes_even_with_write_privileges(
    cluster: PgCluster, db: str, table: str
) -> None:
    """Grant anon every write back, and row level security alone still holds."""
    load_sample(cluster, db)
    grant = cluster.psql(f"grant insert, update, delete on public.{table} to anon;", database=db)
    assert grant.ok, grant.stderr
    statements = write_statements(table)

    inserted = cluster.psql(f"set role anon;\n{statements['insert']}", database=db)
    assert not inserted.ok
    assert "row-level security" in inserted.stderr

    # An update or delete with no policy admitting it matches no rows. Postgres
    # reports success with a zero count, and nothing changes.
    before = table_md5(cluster, db, table)
    updated = cluster.psql(f"set role anon;\n{statements['update']}", database=db)
    assert updated.ok and "UPDATE 0" in updated.stdout
    deleted = cluster.psql(f"set role anon;\n{statements['delete']}", database=db)
    assert deleted.ok and "DELETE 0" in deleted.stdout
    assert table_md5(cluster, db, table) == before


def test_service_role_upserts_on_the_natural_key_and_deletes(cluster: PgCluster, db: str) -> None:
    load_sample(cluster, db)
    load_sample(cluster, db)  # a second identical upsert is a no-op on content
    assert scalar(cluster, db, "select count(*) from public.runs") == "1"
    changed = sample_rows()[schema.RUNS][0] | {"content_sha256": "f" * 64}
    done = cluster.psql(
        f"set role service_role;\n{upsert_sql(schema.RUNS, [changed])}", database=db
    )
    assert done.ok, done.stderr
    assert scalar(cluster, db, "select content_sha256 from public.runs") == "f" * 64
    assert scalar(cluster, db, "select attribution_result is null from public.runs") == "t"

    deleted = cluster.psql("set role service_role;\ndelete from public.runs;", database=db)
    assert deleted.ok and "DELETE 1" in deleted.stdout
    truncated = cluster.psql("set role service_role;\ntruncate public.batches;", database=db)
    assert not truncated.ok and "permission denied" in truncated.stderr


@pytest.mark.parametrize(
    "change",
    [
        {"summary": {"run_id": "other", "task_id": "task_a", "batch_id": "batch_a"}},
        {"batch_id": None},
        {"run_result": {"run_id": "other"}},
        {"trace": {"step": 0}},
        {"failure_card": {"card": 1}},
        {"content_sha256": "not-a-hash"},
    ],
    ids=["summary-key", "batch-filter", "result-key", "trace-shape", "half-bundle", "hash"],
)
def test_inconsistent_run_rows_are_rejected(cluster: PgCluster, db: str, change: dict) -> None:
    row = sample_rows()[schema.RUNS][0] | change
    done = cluster.psql(f"set role service_role;\n{upsert_sql(schema.RUNS, [row])}", database=db)
    assert not done.ok
    assert "violates check constraint" in done.stderr


# A key missing from the JSON makes ->> yield null, and a check whose
# expression is null passes. Every key check compares with "is not distinct
# from", so each of these must be refused.
MISSING_KEYS = [
    (schema.RUNS, {"summary": {"task_id": "task_a", "batch_id": "batch_a"}}),
    (schema.RUNS, {"summary": {"run_id": "run_a", "batch_id": "batch_a"}}),
    (schema.RUNS, {"summary": [{"run_id": "run_a"}]}),
    (schema.RUNS, {"run_result": {"status": "completed"}}),
    (schema.BATCHES, {"summary": {"runs": []}}),
    (schema.BATCHES, {"suite_report": {"rows": []}}),
    (schema.EXPERIMENTS, {"spec": {"hypothesis": "x"}}),
    (schema.EXPERIMENTS, {"result": {"decision": "keep"}}),
]


@pytest.mark.parametrize(
    ("table", "change"),
    MISSING_KEYS,
    ids=[
        "runs-summary-run_id",
        "runs-summary-task_id",
        "runs-summary-not-an-object",
        "runs-result-run_id",
        "batches-summary-batch_id",
        "batches-report-batch_id",
        "experiments-spec-experiment_id",
        "experiments-result-experiment_id",
    ],
)
def test_rows_whose_json_lacks_the_key_are_rejected(
    cluster: PgCluster, db: str, table: str, change: dict
) -> None:
    row = sample_rows()[table][0] | change
    done = cluster.psql(f"set role service_role;\n{upsert_sql(table, [row])}", database=db)
    assert not done.ok, f"{table} accepted {change}"
    assert "violates check constraint" in done.stderr
    assert scalar(cluster, db, f"select count(*) from public.{table}") == "0"


KEY_COLUMNS = [(table, key) for table, key in schema.PRIMARY_KEYS.items()]


def test_every_natural_key_uses_the_c_collation(cluster: PgCluster, migrated: str) -> None:
    pairs = ", ".join(f"('{table}', '{key}')" for table, key in KEY_COLUMNS)
    collations = scalar(
        cluster,
        migrated,
        "select string_agg(c.relname || '.' || a.attname || '=' || co.collname, ',' "
        "order by c.relname) from pg_attribute a "
        "join pg_class c on c.oid = a.attrelid "
        "join pg_namespace n on n.oid = c.relnamespace and n.nspname = 'public' "
        "join pg_collation co on co.oid = a.attcollation "
        f"where (c.relname::text, a.attname::text) in ({pairs})",
    )
    assert sorted(collations.split(",")) == sorted(f"{t}.{k}=C" for t, k in KEY_COLUMNS)


def test_runs_list_in_code_point_order_under_a_linguistic_default(cluster: PgCluster) -> None:
    """A hosted project's database defaults to a linguistic collation.

    RunReader lists runs in Python's code point order, and SupabaseRunReader
    asks PostgREST for order=run_id.asc, so the key column has to sort the same
    way whatever the database default is.
    """
    created = cluster.psql(
        "create database results_icu template template0 "
        "locale_provider icu icu_locale 'en-US' locale 'C';",
        database="postgres",
    )
    if not created.ok:
        pytest.skip(f"this server cannot create an ICU database: {created.stderr.strip()}")
    boot = cluster.psql(SUPABASE_BOOTSTRAP, database="results_icu")
    assert boot.ok, boot.stderr
    for migration in MIGRATIONS:
        applied = cluster.apply_file(migration, database="results_icu")
        assert applied.ok, applied.stderr
    run_ids = ["run_b", "run_B", "Run_a", "run-a", "run_a", "run_10", "run_9"]
    template = sample_rows()[schema.RUNS][0]
    rows = [
        template
        | {
            "run_id": run_id,
            "summary": template["summary"] | {"run_id": run_id},
            "run_result": {"run_id": run_id},
        }
        for run_id in run_ids
    ]
    done = cluster.psql(
        f"set role service_role;\n{upsert_sql(schema.RUNS, rows)}", database="results_icu"
    )
    assert done.ok, done.stderr
    listed = scalar(
        cluster, "results_icu", "select string_agg(run_id, ',' order by run_id) from public.runs"
    )
    linguistic = scalar(
        cluster,
        "results_icu",
        "select string_agg(run_id, ',' order by run_id collate \"default\") from public.runs",
    )
    assert listed.split(",") == sorted(run_ids)
    assert linguistic.split(",") != sorted(run_ids), "the database default is not linguistic"


def test_retained_results_round_trip_through_postgres_as_anon(
    cluster: PgCluster, db: str, tmp_path: Path
) -> None:
    """Every retained item, written as service_role and read back as anon.

    The rows go in through the statement PostgREST runs for an upsert and come
    back through json_agg under row level security, so jsonb storage, the
    grants and the check constraints are all the real ones.
    """
    fs, rows = fake.retained_rows(tmp_path / "staged")
    server = fake.PsqlPostgrest(cluster, db)
    writer = PostgrestClient(fake.BASE_URL, fake.SERVICE_KEY, transport=server)
    for table, table_rows in rows.items():
        writer.upsert(table, table_rows, on_conflict=schema.PRIMARY_KEYS[table])

    hosted = SupabaseRunReader(
        PostgrestClient(fake.BASE_URL, fake.ANON_KEY, transport=server, page_size=4)
    )
    fake.assert_same_reads(fs, hosted, [r["batch_id"] for r in rows[schema.BATCHES]])

    anon = PostgrestClient(fake.BASE_URL, fake.ANON_KEY, transport=server)
    before = table_md5(cluster, db, schema.RUNS)
    with pytest.raises(PostgrestError) as refused:
        anon.upsert(schema.RUNS, rows[schema.RUNS][:1], on_conflict="run_id")
    assert (refused.value.status, refused.value.code) == (401, "42501")
    with pytest.raises(PostgrestError) as refused:
        anon.delete(schema.RUNS, "run_id", [rows[schema.RUNS][0]["run_id"]])
    assert (refused.value.status, refused.value.code) == (401, "42501")
    assert table_md5(cluster, db, schema.RUNS) == before


def test_a_second_upload_leaves_every_tuple_untouched(
    cluster: PgCluster, db: str, tmp_path: Path
) -> None:
    """Idempotence on the real tables. xmin changes whenever Postgres rewrites a row."""
    _, rows = fake.retained_rows(tmp_path / "staged")
    server = fake.PsqlPostgrest(cluster, db)
    client = PostgrestClient(fake.BASE_URL, fake.SERVICE_KEY, transport=server)
    upload(client, rows, prune=True)

    def state() -> list[str]:
        return [
            scalar(
                cluster,
                db,
                f"select string_agg(xmin::text || ':' || md5(t::text), ',' order by t::text) "
                f"from public.{table} t",
            )
            for table in schema.PRIMARY_KEYS
        ]

    before, requests = state(), len(server.requests)
    plans = upload(client, rows, prune=True)
    assert state() == before
    assert [p.method for p in server.requests[requests:]] == ["GET"] * 4
    assert all(p.unchanged == p.retained for p in plans)


# --- lockstep: runs without a database -------------------------------------


def _create_table_columns(sql: str) -> dict[str, tuple[str, ...]]:
    """Column names per table, read from the column lines of each create table."""
    tables: dict[str, tuple[str, ...]] = {}
    for match in re.finditer(r"create table public\.(\w+) \((.*?)\n\);", sql, re.S):
        body = match.group(2)
        tables[match.group(1)] = tuple(
            re.findall(r"^    (\w+) (?:text|jsonb|timestamptz)\b", body, re.M)
        )
    return tables


def test_python_names_match_the_migrations() -> None:
    assert MIGRATIONS, "no migrations found"
    for migration in MIGRATIONS:
        assert re.fullmatch(r"\d{14}_[a-z0-9_]+\.sql", migration.name), migration.name
    sql = "\n".join(m.read_text(encoding="utf-8") for m in MIGRATIONS)
    assert _create_table_columns(sql) == schema.COLUMNS
    recorded = re.findall(r"insert into public\.schema_versions .*?values \('([^']+)'", sql, re.S)
    assert recorded and recorded[-1] == schema.RESULTS_SCHEMA_VERSION
    newest = MIGRATIONS[-1].name
    assert newest.endswith("_" + schema.RESULTS_SCHEMA_VERSION.replace(".", "_") + ".sql")
