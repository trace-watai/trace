"""PostgREST stand-ins for the public results tests. None of them opens a socket.

There is no Supabase project yet, so nothing here was recorded from one. Every
response is synthesized in the shape PostgREST gives. Reads return JSON arrays
of row objects, carrying only the selected columns. Paged reads carry a
``Content-Range`` header with the total when ``Prefer: count=exact`` is sent,
and use status 206 for a partial page. Errors come as ``{code, details, hint,
message}`` bodies with PostgREST's status codes. Only the subset of PostgREST
the harness uses is modelled.

``MemoryPostgrest``
    Tables held in dicts, with Supabase's key-to-role mapping and a simple
    model of what each role may do. Always available.
``PsqlPostgrest``
    The same requests translated to SQL and run through ``psql`` against the
    throwaway cluster from ``pg_cluster.py``, as the role the key maps to. Row
    level security, grants, check constraints and jsonb storage are then the
    real ones.
``RecordingTransport`` / ``ReplayTransport``
    Record exchanges into, and answer from, the committed fixture
    ``fixtures/postgrest/synthesized_retained.json``.

Regenerate the committed fixture from the repository root with

    PYTHONPATH=src:tests python tests/postgrest_fake.py --write-fixture
"""

from __future__ import annotations

import base64
import json
import re
import shutil
import sys
import tempfile
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from trace_harness.public_results import schema
from trace_harness.public_results.postgrest import HttpRequest, HttpResponse, key_role
from trace_harness.run_reader import RunNotFound, RunReader

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "postgrest" / "synthesized_retained.json"

# A host under the reserved .invalid top-level domain can never resolve.
BASE_URL = "https://synthesized.supabase.invalid"
# Short, obviously fake keys in the new formats. Legacy JWT keys are built at
# run time by make_jwt so no token-shaped literal sits in the repository.
ANON_KEY = "sb_publishable_TEST"
SERVICE_KEY = "sb_secret_TEST"

TABLES = (schema.SCHEMA_VERSIONS, *schema.PRIMARY_KEYS)


def make_jwt(role: str) -> str:
    """A legacy-format key for ``role``. The signature is not a real one."""

    def part(obj: dict[str, Any]) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{part({'alg': 'HS256', 'typ': 'JWT'})}.{part({'role': role})}.c2lnbmF0dXJl"


# The keys the stand-ins accept, and the role each maps to, as Supabase's
# gateway would. Any other key is refused with 401.
VALID_KEYS = {
    ANON_KEY: "anon",
    SERVICE_KEY: "service_role",
    make_jwt("anon"): "anon",
    make_jwt("service_role"): "service_role",
}


def error_response(status: int, code: str | None, message: str, hint: str | None = None):
    body = {"code": code, "details": None, "hint": hint, "message": message}
    return HttpResponse(
        status, {"content-type": "application/json; charset=utf-8"}, json.dumps(body).encode()
    )


def json_response(status: int, payload: Any, content_range: str | None = None) -> HttpResponse:
    headers = {"content-type": "application/json; charset=utf-8"}
    if content_range is not None:
        headers["content-range"] = content_range
    return HttpResponse(status, headers, json.dumps(payload, separators=(",", ":")).encode())


# --- request parsing --------------------------------------------------------


@dataclass
class Parsed:
    method: str
    table: str
    role: str | None
    select: list[str] = field(default_factory=list)
    filters: list[tuple[str, str, list[str]]] = field(default_factory=list)
    order: tuple[str, str] | None = None
    limit: int | None = None
    offset: int = 0
    on_conflict: str | None = None
    prefer: set[str] = field(default_factory=set)
    body: Any = None


def _split_in_list(raw: str) -> list[str]:
    inner = raw[len("in.(") : -1]
    return [
        v.replace('\\"', '"').replace("\\\\", "\\")
        for v in re.findall(r'"((?:[^"\\]|\\.)*)"', inner)
    ] or [v for v in inner.split(",") if v]


def parse(request: HttpRequest) -> Parsed | HttpResponse:
    """Parse a request, or return the error response PostgREST or the gateway would."""
    url = urllib.parse.urlsplit(request.url)
    headers = {k.lower(): v for k, v in request.headers.items()}
    key = headers.get("apikey", "")
    role = VALID_KEYS.get(key)
    if role is None or key_role(key) != role:
        return error_response(401, None, "Invalid API key", "Double check your Supabase API key.")
    auth = headers.get("authorization")
    if key.startswith("eyJ") and auth != f"Bearer {key}":
        return error_response(401, "PGRST301", "JWT missing from Authorization")
    if not key.startswith("eyJ") and auth is not None:
        return error_response(401, "PGRST301", "Expected 3 parts in JWT; got 1")
    match = re.fullmatch(r"/rest/v1/(\w+)", url.path)
    if not match:
        return error_response(404, None, "not found")
    table = match.group(1)
    if table not in TABLES:
        return error_response(
            404, "PGRST205", f"Could not find the table 'public.{table}' in the schema cache"
        )
    parsed = Parsed(request.method, table, role)
    parsed.prefer = {p.strip() for p in headers.get("prefer", "").split(",") if p.strip()}
    for name, value in urllib.parse.parse_qsl(url.query, keep_blank_values=True):
        if name == "select":
            parsed.select = value.split(",")
        elif name == "order":
            column, _, direction = value.partition(".")
            parsed.order = (column, direction or "asc")
        elif name == "limit":
            parsed.limit = int(value)
        elif name == "offset":
            parsed.offset = int(value)
        elif name == "on_conflict":
            parsed.on_conflict = value
        elif value.startswith("eq."):
            parsed.filters.append((name, "eq", [value[3:]]))
        elif value.startswith("in.(") and value.endswith(")"):
            parsed.filters.append((name, "in", _split_in_list(value)))
        else:
            return error_response(400, "PGRST100", f"failed to parse filter ({name}={value})")
    columns = schema.COLUMNS[table]
    named = (
        parsed.select + [f[0] for f in parsed.filters] + ([parsed.order[0]] if parsed.order else [])
    )
    for column in named:
        if column not in columns:
            return error_response(400, "42703", f"column {table}.{column} does not exist")
    if request.body is not None:
        parsed.body = json.loads(request.body)
    return parsed


def _content_range(offset: int, count: int, total: int | None) -> str:
    shown = "*" if count == 0 else f"{offset}-{offset + count - 1}"
    return f"{shown}/{'*' if total is None else total}"


def _read_status(offset: int, count: int, total: int | None) -> int:
    if total is None or (offset == 0 and count == total):
        return 200
    return 206


# --- in memory ----------------------------------------------------------------


class MemoryPostgrest:
    """An in-memory PostgREST with Supabase's role rules, used as a transport."""

    def __init__(self, *, max_rows: int = 1000):
        self.tables: dict[str, dict[str, dict[str, Any]]] = {t: {} for t in TABLES}
        self.tables[schema.SCHEMA_VERSIONS][schema.RESULTS_SCHEMA_VERSION] = {
            "version": schema.RESULTS_SCHEMA_VERSION,
            "description": "synthesized",
            "applied_at": "2026-09-23T12:00:00+00:00",
        }
        self.max_rows = max_rows
        self.requests: list[Parsed] = []

    def load(self, rows_by_table: dict[str, list[dict[str, Any]]]) -> MemoryPostgrest:
        for table, rows in rows_by_table.items():
            key = schema.PRIMARY_KEYS[table]
            for row in rows:
                self.tables[table][row[key]] = json.loads(json.dumps(row))
        return self

    def snapshot(self) -> str:
        return json.dumps(self.tables, sort_keys=True)

    def writes(self) -> list[Parsed]:
        return [p for p in self.requests if p.method in {"POST", "PATCH", "DELETE"}]

    def __call__(self, request: HttpRequest) -> HttpResponse:
        parsed = parse(request)
        if isinstance(parsed, HttpResponse):
            return parsed
        self.requests.append(parsed)
        if parsed.method == "GET":
            return self._get(parsed)
        if parsed.role != "service_role" or parsed.table == schema.SCHEMA_VERSIONS:
            return error_response(401, "42501", f"permission denied for table {parsed.table}")
        if parsed.method == "POST":
            return self._upsert(parsed)
        if parsed.method == "DELETE":
            return self._delete(parsed)
        return error_response(405, None, f"{parsed.method} is not modelled")

    def _matches(self, row: dict[str, Any], parsed: Parsed) -> bool:
        for column, _op, values in parsed.filters:
            value = row.get(column)
            if value is None or str(value) not in values:
                return False
        return True

    def _get(self, parsed: Parsed) -> HttpResponse:
        rows = [r for r in self.tables[parsed.table].values() if self._matches(r, parsed)]
        if parsed.order:
            column, direction = parsed.order
            rows.sort(key=lambda r: r[column], reverse=direction == "desc")
        else:
            # PostgREST promises no order without one. Reversing load order
            # makes a reader that forgets to ask visibly wrong.
            rows.reverse()
        total = len(rows) if "count=exact" in parsed.prefer else None
        limit = min(parsed.limit or self.max_rows, self.max_rows)
        page = rows[parsed.offset : parsed.offset + limit]
        body = [{c: r.get(c) for c in parsed.select} for r in page]
        return json_response(
            _read_status(parsed.offset, len(page), total),
            body,
            _content_range(parsed.offset, len(page), total),
        )

    def _upsert(self, parsed: Parsed) -> HttpResponse:
        key = schema.PRIMARY_KEYS[parsed.table]
        rows = parsed.body if isinstance(parsed.body, list) else [parsed.body]
        merge = "resolution=merge-duplicates" in parsed.prefer and parsed.on_conflict == key
        for row in rows:
            unknown = set(row) - set(schema.COLUMNS[parsed.table])
            if unknown:
                column = sorted(unknown)[0]
                return error_response(
                    400,
                    "PGRST204",
                    f"Could not find the '{column}' column of '{parsed.table}' in the schema cache",
                )
            if row[key] in self.tables[parsed.table] and not merge:
                return error_response(
                    409,
                    "23505",
                    f'duplicate key value violates unique constraint "{parsed.table}_pkey"',
                )
        for row in rows:
            self.tables[parsed.table][row[key]] = json.loads(json.dumps(row))
        return HttpResponse(201, {}, b"")

    def _delete(self, parsed: Parsed) -> HttpResponse:
        if not parsed.filters:
            return error_response(400, "21000", "DELETE requires a WHERE clause")
        doomed = [k for k, r in self.tables[parsed.table].items() if self._matches(r, parsed)]
        for k in doomed:
            del self.tables[parsed.table][k]
        return HttpResponse(204, {}, b"")


# --- over a real Postgres -------------------------------------------------------


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


class PsqlPostgrest:
    """PostgREST's reads and writes as SQL, run through psql as the key's role."""

    def __init__(self, cluster: Any, database: str):
        self.cluster = cluster
        self.database = database
        self.requests: list[Parsed] = []

    def __call__(self, request: HttpRequest) -> HttpResponse:
        parsed = parse(request)
        if isinstance(parsed, HttpResponse):
            return parsed
        self.requests.append(parsed)
        where = " and ".join(
            f"{column} = {_literal(values[0])}"
            if op == "eq"
            else f"{column} in ({', '.join(_literal(v) for v in values)})"
            for column, op, values in parsed.filters
        )
        where = f" where {where}" if where else ""
        table = f"public.{parsed.table}"
        if parsed.method == "GET":
            order = f" order by {parsed.order[0]} {parsed.order[1]}" if parsed.order else ""
            limit = f" limit {parsed.limit}" if parsed.limit is not None else ""
            columns = ", ".join(parsed.select)
            sql = (
                f"select json_build_object('total', (select count(*) from {table}{where}), "
                f"'rows', coalesce((select json_agg(x) from (select {columns} from {table}"
                f"{where}{order}{limit} offset {parsed.offset}) x), '[]'::json));"
            )
            done = self._run(parsed.role, sql, tuples_only=True)
            if isinstance(done, HttpResponse):
                return done
            data = json.loads(done)
            total = data["total"] if "count=exact" in parsed.prefer else None
            count = len(data["rows"])
            return json_response(
                _read_status(parsed.offset, count, total),
                data["rows"],
                _content_range(parsed.offset, count, total),
            )
        if parsed.method == "POST":
            columns = ", ".join(schema.COLUMNS[parsed.table])
            key = parsed.on_conflict or schema.PRIMARY_KEYS[parsed.table]
            updates = ", ".join(
                f"{c} = excluded.{c}" for c in schema.COLUMNS[parsed.table] if c != key
            )
            body = json.dumps(parsed.body)
            tag = "$body$" if "$body$" not in body else "$body_2$"
            conflict = (
                f" on conflict ({key}) do update set {updates}"
                if "resolution=merge-duplicates" in parsed.prefer
                else ""
            )
            sql = (
                f"insert into {table} ({columns}) select {columns} from "
                f"jsonb_populate_recordset(null::{table}, {tag}{body}{tag}::jsonb){conflict};"
            )
            done = self._run(parsed.role, sql)
            return done if isinstance(done, HttpResponse) else HttpResponse(201, {}, b"")
        if parsed.method == "DELETE":
            if not where:
                return error_response(400, "21000", "DELETE requires a WHERE clause")
            done = self._run(parsed.role, f"delete from {table}{where};")
            return done if isinstance(done, HttpResponse) else HttpResponse(204, {}, b"")
        return error_response(405, None, f"{parsed.method} is not modelled")

    def _run(self, role: str | None, sql: str, *, tuples_only: bool = False) -> str | HttpResponse:
        done = self.cluster.psql(
            f"set role {role};\n{sql}", database=self.database, tuples_only=tuples_only
        )
        if done.ok:
            return done.stdout.strip()
        message = done.stderr.strip().splitlines()[0].removeprefix("psql:<stdin>:2: ERROR:  ")
        message = re.sub(r"^psql:[^ ]+ ERROR:\s+", "", message)
        if "permission denied" in message or "row-level security" in message:
            # PostgREST answers 42501 with 401 for anon and 403 otherwise.
            return error_response(401 if role == "anon" else 403, "42501", message)
        if "violates check constraint" in message:
            return error_response(400, "23514", message)
        return error_response(400, None, message)


# --- recording and replay ---------------------------------------------------


class RecordingTransport:
    """Pass requests to ``inner`` and keep each exchange for the fixture."""

    def __init__(self, inner: Any):
        self.inner = inner
        self.exchanges: list[dict[str, Any]] = []

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.inner(request)
        self.exchanges.append(
            {
                "request": {"method": request.method, "url": request.url},
                "response": {
                    "status": response.status,
                    "headers": dict(response.headers),
                    "body": json.loads(response.body) if response.body else None,
                },
            }
        )
        return response


class ReplayTransport:
    """Answer only the requests the fixture holds, the way a cassette does."""

    def __init__(self, fixture: dict[str, Any]):
        self.exchanges = {
            (e["request"]["method"], e["request"]["url"]): e["response"]
            for e in fixture["exchanges"]
        }
        self.used: set[tuple[str, str]] = set()

    def __call__(self, request: HttpRequest) -> HttpResponse:
        key = (request.method, request.url)
        if key not in self.exchanges:
            raise AssertionError(f"no synthesized response for {request.method} {request.url}")
        if not request.headers.get("apikey"):
            raise AssertionError("request sent without an apikey header")
        self.used.add(key)
        recorded = self.exchanges[key]
        body = b"" if recorded["body"] is None else json.dumps(recorded["body"]).encode()
        return HttpResponse(recorded["status"], dict(recorded["headers"]), body)


# --- parity between the backends ----------------------------------------------


def without_generated_at(report: Any) -> dict:
    return report.model_dump(mode="json") | {"generated_at": None}


def assert_same_reads(
    fs: Any, hosted: Any, batch_ids: list[str], bundle_refs: dict[str, str] | None = None
) -> None:
    """Every RunReader method on every retained id, plus an unknown id of each kind.

    A reproduction in ``bundle_refs`` gets the bundle of the run it names, as
    RunReader.get_bundle has it since #211.
    """
    bundle_refs = bundle_refs or {}
    summaries = fs.list_runs()
    assert hosted.list_runs() == summaries
    for summary in summaries:
        run_id = summary.run_id
        for method in ("get_run", "get_task", "get_trace", "get_verifier", "get_attribution"):
            assert getattr(hosted, method)(run_id) == getattr(fs, method)(run_id), (method, run_id)
        bundle_home = bundle_refs.get(run_id, run_id)
        assert hosted.get_bundle(run_id) == fs.get_bundle(bundle_home), ("get_bundle", run_id)
    for method in ("get_run", "get_task", "get_trace", "get_verifier", "get_bundle"):
        with pytest.raises(RunNotFound):
            getattr(fs, method)("run_not_retained")
        with pytest.raises(RunNotFound):
            getattr(hosted, method)("run_not_retained")

    for batch_id in [*batch_ids, "batch_not_retained"]:
        assert hosted.list_runs_for_batch(batch_id) == fs.list_runs_for_batch(batch_id)
    for batch_id in batch_ids:
        assert hosted.get_batch_summary(batch_id) == fs.get_batch_summary(batch_id)
        assert without_generated_at(hosted.get_suite_report(batch_id)) == without_generated_at(
            fs.get_suite_report(batch_id)
        )
    for method in ("get_batch_summary", "get_suite_report"):
        with pytest.raises(FileNotFoundError, match="batch summary not found"):
            getattr(fs, method)("batch_not_retained")
        with pytest.raises(FileNotFoundError, match="batch summary not found"):
            getattr(hosted, method)("batch_not_retained")

    assert hosted.list_experiments() == fs.list_experiments()
    for spec in fs.list_experiments():
        assert hosted.get_experiment(spec.experiment_id) == fs.get_experiment(spec.experiment_id)
    for reader in (fs, hosted):
        with pytest.raises(FileNotFoundError, match="experiment.json not found"):
            reader.get_experiment("exp_not_retained")


# --- the committed fixture ----------------------------------------------------

FIXTURE_NOTE = (
    "Synthesized PostgREST responses. No Supabase project existed when this was written, "
    "so nothing here was recorded from a live one. The rows are built through RunReader "
    "from the real retained artifacts under docs/acceptance/ (public_results.rows), "
    "loaded into tests/postgrest_fake.py's MemoryPostgrest, and each exchange is what "
    "SupabaseRunReader asked and what that stand-in answered, shaped like PostgREST "
    "output (JSON arrays of selected columns, Content-Range with the exact count, 206 "
    "for a partial page, error bodies with code, details, hint and message). "
    "Regenerate with: PYTHONPATH=src:tests python tests/postgrest_fake.py --write-fixture"
)

FULL_CHAIN_RUN = "run_20260820T012748Z_0e9c6172"
PASSING_RUN = "run_20260913T141412Z_3e4b44a9"
FIXTURE_BATCH = "batch_20260917T191429Z_87bfa2c7"
MEMBER_BATCH = "batch_20260820T012748Z_31f2f0ec"
FIXTURE_EXPERIMENT = "exp_000_baseline"
FIXTURE_PAGE_SIZE = 10


# --- a retained tree with a reproduction (#211) ---------------------------------

# Two retained runs that both carry a full bundle. In the tree below the second
# becomes a reproduction of the first: its own bundle files are removed and a
# bundle_ref.json names the first, as the bundle stage writes it since #211.
CARD_RUN = FULL_CHAIN_RUN
REPRODUCTION_RUN = "run_20260913T150039Z_0f2f19b7"
BUNDLE_FILES = ("failure_card.json", "repair_package.json", "regression_artifact.json")
BUNDLE_COLUMNS = ("failure_card", "repair_package", "regression_artifact")


def retained_run_dir(run_id: str) -> Path:
    return next(
        p.parent
        for p in (REPO_ROOT / "docs" / "acceptance").rglob("run_result.json")
        if p.parent.name == run_id
    )


def bundle_ref(run_id: str, canonical_run_id: Any) -> dict[str, Any]:
    """bundle_ref.json in BundleRef 0.1.0's shape (#211)."""
    return {
        "schema_version": "0.1.0",
        "run_id": run_id,
        "task_id": json.loads((retained_run_dir(run_id) / "run_result.json").read_text())[
            "task_id"
        ],
        "bundle_key": "synthesized-bundle-key",
        "canonical_run_id": canonical_run_id,
    }


def retained_with_reproduction(
    root: Path, *, canonical_run_id: Any = CARD_RUN, with_card_run: bool = True
) -> Path:
    """A retained tree holding the card run and a reproduction pointing at it."""
    if with_card_run:
        shutil.copytree(retained_run_dir(CARD_RUN), root / "runs" / CARD_RUN)
    reproduction = root / "live" / REPRODUCTION_RUN
    shutil.copytree(retained_run_dir(REPRODUCTION_RUN), reproduction)
    for name in BUNDLE_FILES:
        (reproduction / name).unlink()
    (reproduction / "bundle_ref.json").write_text(
        json.dumps(bundle_ref(REPRODUCTION_RUN, canonical_run_id)), encoding="utf-8"
    )
    return root


class BundleRefReader(RunReader):
    """RunReader whose get_bundle follows bundle_ref.json, as it does since #211.

    This branch predates #211, so its RunReader returns None for a
    reproduction. Rows must come out the same with either reader.
    """

    def get_bundle(self, run_id: str) -> Any:
        ref = self.store.run_dir(run_id) / "bundle_ref.json"
        if ref.is_file() and not self.store.exists(run_id, "failure_card.json"):
            return super().get_bundle(json.loads(ref.read_text("utf-8"))["canonical_run_id"])
        return super().get_bundle(run_id)


def reproduction_rows(root: Path, staging: Path) -> tuple[Any, Any, dict[str, list[dict]]]:
    """Stage ``root`` and build rows with a reader that follows pointers."""
    from trace_harness.public_results.retained import stage_retained
    from trace_harness.public_results.rows import build_rows

    staged = stage_retained(root, staging)
    reader = BundleRefReader.from_runs_dir(staged.runs_dir)
    return reader, staged, build_rows(reader, sorted(staged.batches), staged.bundle_refs)


def retained_rows(staging: Path) -> tuple[Any, dict[str, list[dict[str, Any]]]]:
    """Stage the retained tree into ``staging`` and build every row through RunReader."""
    from trace_harness.public_results.retained import stage_retained
    from trace_harness.public_results.rows import build_rows
    from trace_harness.run_reader import RunReader

    staged = stage_retained(REPO_ROOT / "docs" / "acceptance", staging)
    reader = RunReader.from_runs_dir(staged.runs_dir)
    return reader, build_rows(reader, sorted(staged.batches), staged.bundle_refs)


def exercise_fixture_reads(reader: Any) -> None:
    """The reads the committed fixture holds, in order. Missing ids are expected."""
    reader.list_runs()
    reader.list_runs_for_batch(MEMBER_BATCH)
    reader.list_runs_for_batch("batch_not_retained")
    for get in (
        "get_run",
        "get_task",
        "get_trace",
        "get_verifier",
        "get_attribution",
        "get_bundle",
    ):
        getattr(reader, get)(FULL_CHAIN_RUN)
    for get in ("get_verifier", "get_attribution", "get_bundle"):
        getattr(reader, get)(PASSING_RUN)
    reader.get_batch_summary(FIXTURE_BATCH)
    reader.get_suite_report(FIXTURE_BATCH)
    reader.list_experiments()
    reader.get_experiment(FIXTURE_EXPERIMENT)
    for call, arg in (
        ("get_run", "run_not_retained"),
        ("get_batch_summary", "batch_not_retained"),
        ("get_experiment", "exp_not_retained"),
    ):
        try:
            getattr(reader, call)(arg)
        except FileNotFoundError:
            pass


def build_fixture() -> dict[str, Any]:
    from trace_harness.public_results.postgrest import PostgrestClient
    from trace_harness.run_reader_supabase import SupabaseRunReader

    with tempfile.TemporaryDirectory() as tmp:
        _, rows = retained_rows(Path(tmp) / "staged")
    server = MemoryPostgrest().load(rows)
    recorder = RecordingTransport(server)
    client = PostgrestClient(BASE_URL, ANON_KEY, transport=recorder, page_size=FIXTURE_PAGE_SIZE)
    exercise_fixture_reads(SupabaseRunReader(client))
    # Two refusals the reader must surface: a wrong key, and a project whose
    # migration has not been applied.
    bad_key = PostgrestClient(BASE_URL, "sb_publishable_WRONG", transport=recorder)
    bad_key._send("GET", schema.RUNS, [("select", "summary"), ("run_id", "eq.x")])  # noqa: SLF001
    anon = PostgrestClient(BASE_URL, ANON_KEY, transport=recorder)
    anon._send("GET", "runz", [("select", "summary")])  # noqa: SLF001
    return {"note": FIXTURE_NOTE, "base_url": BASE_URL, "exchanges": recorder.exchanges}


def main(argv: list[str]) -> int:
    if argv != ["--write-fixture"]:
        print(__doc__)
        return 2
    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_PATH.write_text(json.dumps(build_fixture(), indent=1) + "\n", encoding="utf-8")
    print(f"wrote {FIXTURE_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
