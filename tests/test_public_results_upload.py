"""The uploader and the key scan that runs before it.

The uploader is driven against the in-memory PostgREST stand-in from
tests/postgrest_fake.py, loaded with rows built from the real retained tree.
Nothing opens a socket. Idempotence is checked on the stand-in here and on the
temp Postgres cluster in test_public_results_sql.py.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from pathlib import Path

import pytest

import postgrest_fake as fake
from trace_harness.public_results import schema, secret_scan
from trace_harness.public_results.postgrest import PostgrestClient, PostgrestError
from trace_harness.public_results.upload import (
    UploadError,
    main,
    prepare_rows,
    request_chunks,
    upload,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ACCEPTANCE = REPO_ROOT / "docs" / "acceptance"
ENV = {"TRACE_SUPABASE_URL": fake.BASE_URL, "TRACE_SUPABASE_SERVICE_KEY": fake.SERVICE_KEY}


@pytest.fixture(scope="module")
def rows(tmp_path_factory: pytest.TempPathFactory) -> dict[str, list[dict]]:
    return prepare_rows(ACCEPTANCE, tmp_path_factory.mktemp("upload") / "staged")


def writer(server: fake.MemoryPostgrest) -> PostgrestClient:
    return PostgrestClient(fake.BASE_URL, fake.SERVICE_KEY, transport=server)


def hosted_equals(server: fake.MemoryPostgrest, rows: dict[str, list[dict]]) -> bool:
    for table, table_rows in rows.items():
        key = schema.PRIMARY_KEYS[table]
        expected = {r[key]: json.loads(json.dumps(r)) for r in table_rows}
        if server.tables[table] != expected:
            return False
    return True


# --- idempotence --------------------------------------------------------------


def test_first_upload_hosts_every_retained_row(rows) -> None:
    server = fake.MemoryPostgrest()
    plans = upload(writer(server), rows)
    assert hosted_equals(server, rows)
    assert all(len(p.inserts) == p.retained and not p.updates for p in plans)
    assert {p.table for p in plans} == set(schema.PRIMARY_KEYS)


def test_a_rerun_sends_no_write_and_changes_nothing(rows) -> None:
    server = fake.MemoryPostgrest()
    upload(writer(server), rows)
    before, writes = server.snapshot(), len(server.writes())

    plans = upload(writer(server), rows, prune=True)

    assert len(server.writes()) == writes, "a rerun wrote"
    assert server.snapshot() == before
    assert all(p.unchanged == p.retained and not p.upserts and not p.orphans for p in plans)


def test_rewriting_every_row_on_its_natural_key_changes_nothing(rows) -> None:
    """If the hash check were bypassed, the upsert itself is still idempotent."""
    server = fake.MemoryPostgrest()
    upload(writer(server), rows)
    before = server.snapshot()
    client = writer(server)
    for table, table_rows in rows.items():
        client.upsert(table, table_rows, on_conflict=schema.PRIMARY_KEYS[table])
    assert server.snapshot() == before


def test_only_changed_rows_are_rewritten(rows) -> None:
    server = fake.MemoryPostgrest()
    upload(writer(server), rows)
    stale_id = rows[schema.RUNS][3]["run_id"]
    server.tables[schema.RUNS][stale_id]["content_sha256"] = "0" * 64
    server.tables[schema.RUNS][stale_id]["summary"] = {"stale": True}
    writes = len(server.writes())

    plans = upload(writer(server), rows)

    runs = next(p for p in plans if p.table == schema.RUNS)
    assert [r["run_id"] for r in runs.updates] == [stale_id] and not runs.inserts
    posted = server.writes()[writes:]
    assert [(p.method, p.table, [r["run_id"] for r in p.body]) for p in posted] == [
        ("POST", schema.RUNS, [stale_id])
    ]
    assert hosted_equals(server, rows)


def test_rows_no_longer_retained_are_pruned_only_when_asked(rows) -> None:
    server = fake.MemoryPostgrest()
    upload(writer(server), rows)
    ghost = rows[schema.EXPERIMENTS][0] | {"experiment_id": "exp_removed_from_main"}
    server.tables[schema.EXPERIMENTS]["exp_removed_from_main"] = ghost

    plans = upload(writer(server), rows)
    assert next(p for p in plans if p.table == schema.EXPERIMENTS).orphans == [
        "exp_removed_from_main"
    ]
    assert "exp_removed_from_main" in server.tables[schema.EXPERIMENTS]

    upload(writer(server), rows, prune=True)
    assert hosted_equals(server, rows)


def test_prune_refuses_an_empty_retained_set(rows) -> None:
    server = fake.MemoryPostgrest()
    upload(writer(server), rows)
    before = server.snapshot()
    with pytest.raises(UploadError, match="retained set is empty"):
        upload(writer(server), {table: [] for table in rows}, prune=True)
    assert server.snapshot() == before


def test_dry_run_plans_without_writing(rows) -> None:
    server = fake.MemoryPostgrest()
    plans = upload(writer(server), rows, dry_run=True)
    assert sum(len(p.inserts) for p in plans) == sum(len(r) for r in rows.values())
    assert not server.writes()


def test_request_bodies_stay_under_the_limit(rows) -> None:
    server = fake.MemoryPostgrest()
    bodies: list[bytes] = []

    def spy(request):
        if request.method == "POST":
            bodies.append(request.body)
        return server(request)

    upload(
        PostgrestClient(fake.BASE_URL, fake.SERVICE_KEY, transport=spy),
        rows,
        max_request_bytes=200_000,
    )
    assert len(bodies) > len(rows)  # the runs table needed several requests
    for body in bodies:
        assert len(body) <= 200_000 or len(json.loads(body)) == 1
    assert hosted_equals(server, rows)
    big = rows[schema.RUNS][0]
    assert request_chunks([big, big], max_bytes=10) == [[big], [big]]


# --- refusals -----------------------------------------------------------------


def test_a_project_without_the_schema_version_is_refused_before_any_write(rows) -> None:
    server = fake.MemoryPostgrest()
    server.tables[schema.SCHEMA_VERSIONS].clear()
    with pytest.raises(UploadError, match=r"does not have public results SQL schema 0\.1\.0"):
        upload(writer(server), rows)
    assert not server.writes()


def test_the_anonymous_key_cannot_write(rows) -> None:
    server = fake.MemoryPostgrest()
    anon = PostgrestClient(fake.BASE_URL, fake.ANON_KEY, transport=server)
    with pytest.raises(PostgrestError) as refused:
        upload(anon, rows)
    assert (refused.value.status, refused.value.code) == (401, "42501")
    assert all(not table for name, table in server.tables.items() if name in schema.PRIMARY_KEYS)


def test_main_publishes_then_reports_nothing_to_do(capsys: pytest.CaptureFixture[str]) -> None:
    server = fake.MemoryPostgrest()
    assert main([str(ACCEPTANCE), "--prune"], env=ENV, transport=server) == 0
    first = capsys.readouterr().out
    assert f"published to {fake.BASE_URL}" in first
    writes = len(server.writes())
    assert main([str(ACCEPTANCE), "--prune"], env=ENV, transport=server) == 0
    second = capsys.readouterr().out
    assert f"nothing to publish, {fake.BASE_URL} already matches" in second
    assert "runs: 15 retained, 0 new, 0 changed, 15 unchanged, 0 pruned" in second
    assert len(server.writes()) == writes
    assert fake.SERVICE_KEY not in first + second


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({}, "TRACE_SUPABASE_URL and TRACE_SUPABASE_SERVICE_KEY must be set"),
        (ENV | {"TRACE_SUPABASE_SERVICE_KEY": fake.ANON_KEY}, "holds the anonymous key"),
        (ENV | {"TRACE_SUPABASE_SERVICE_KEY": fake.make_jwt("anon")}, "holds the anonymous key"),
        (ENV | {"TRACE_SUPABASE_URL": "http://x.supabase.co"}, "https://"),
    ],
    ids=["unset", "publishable", "legacy-anon", "plain-http"],
)
def test_main_refuses_bad_configuration_before_any_request(
    env: dict, message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    server = fake.MemoryPostgrest()
    assert main([str(ACCEPTANCE)], env=env, transport=server) == 2
    assert message in capsys.readouterr().err
    assert not server.requests


def test_main_reports_a_refusal_with_exit_1(capsys: pytest.CaptureFixture[str]) -> None:
    server = fake.MemoryPostgrest()
    server.tables[schema.SCHEMA_VERSIONS].clear()
    assert main([str(ACCEPTANCE)], env=ENV, transport=server) == 1
    assert "Apply the migrations" in capsys.readouterr().err


def test_offline_builds_and_dumps_without_a_request(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def no_network(request):  # pragma: no cover - failing is the point
        raise AssertionError(f"offline mode sent {request.method} {request.url}")

    assert (
        main([str(ACCEPTANCE), "--offline", "--dump", str(tmp_path)], env={}, transport=no_network)
        == 0
    )
    out = capsys.readouterr().out
    assert "runs: 15 rows" in out
    dumped = json.loads((tmp_path / "runs.json").read_text(encoding="utf-8"))
    assert len(dumped) == 15 and tuple(dumped[0]) == schema.COLUMNS[schema.RUNS]


# --- the key scan -------------------------------------------------------------

# Built at run time so no key-shaped literal sits in the repository.
PLANTED = {
    "google api key": "AIza" + "B" * 35,
    "google AQ key": "AQ." + "Ab8RN6" * 5,
    "anthropic key": "sk-ant-" + "api03-" + "x" * 30,
    "openai key": "sk-" + "proj-" + "Y" * 40,
    "supabase secret key": "sb_" + "secret_" + "Z" * 32,
    "supabase access token": "sbp_" + "a1" * 20,
    "jwt": ".".join(["eyJ" + "h" * 20, "eyJ" + "p" * 30, "s" * 43]),
    "github token": "ghp_" + "G" * 36,
    "aws access key id": "AKIA" + "Q" * 16,
    "private key": "-----BEGIN " + "RSA PRIVATE KEY-----",
    "authorization header": '"Authorization": "Bearer ' + "t" * 24 + '"',
    "api key header": '"x-goog-api-key": "' + "k" * 39 + '"',
}


# How a key can sit in a retained file. A cassette or trace stores a model's
# text as a JSON string, sometimes JSON inside JSON, and a URL stores it
# percent-encoded, so the character before the key is often the letter of an
# escape (the n of \\n, the 0 of %20) and a pattern anchored on a word
# boundary would not start there. Each shape is built with the real encoder.
ENCODED = {
    "json newline": lambda secret: json.dumps({"text": "line one\n" + secret}),
    "json tab": lambda secret: json.dumps({"text": "cell\t" + secret}),
    "json in json": lambda secret: json.dumps({"raw": json.dumps({"text": "a\n" + secret})}),
    "json unicode": lambda secret: json.dumps({"text": "caf\u00e9\u00a0" + secret}),
    "percent-encoded": lambda secret: "GET /v1?q=" + urllib.parse.quote("a " + secret, safe=""),
    "percent in json": lambda secret: json.dumps(
        {"url": "https://x.invalid/?q=" + urllib.parse.quote("a\n" + secret, safe="")}
    ),
}


def test_every_pattern_has_a_planted_sample() -> None:
    assert set(PLANTED) == {name for name, _ in secret_scan.PATTERNS}


@pytest.mark.parametrize("name", sorted(PLANTED))
def test_a_planted_key_is_caught_and_never_printed(
    name: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = PLANTED[name]
    cassette = tmp_path / "cassettes" / "provider" / "default.jsonl"
    cassette.parent.mkdir(parents=True)
    cassette.write_text('{"ok": 1}\n{"request": {"note": "x ' + secret + ' y"}}\n', "utf-8")

    assert secret_scan.main([str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert f"default.jsonl:2: {name}" in err
    assert secret not in err


@pytest.mark.parametrize("shape", sorted(ENCODED))
@pytest.mark.parametrize("name", sorted(PLANTED))
def test_a_key_behind_an_escape_is_caught_and_never_printed(
    name: str, shape: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = PLANTED[name]
    line = ENCODED[shape](secret)
    trace = tmp_path / "run_x" / "trace.jsonl"
    trace.parent.mkdir()
    trace.write_text('{"step": 0}\n' + line + "\n", "utf-8")

    hits = secret_scan.scan_text(trace.read_text("utf-8"), "trace.jsonl")
    assert [(h.line, h.pattern) for h in hits if h.pattern == name] == [(2, name)], hits
    assert secret_scan.main([str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert f"trace.jsonl:2: {name}" in err
    assert secret not in err and line not in err


def test_unescape_decodes_json_and_percent_escapes() -> None:
    assert secret_scan.unescape(r"a\nb\tc\u00e9\"d\"\/") == 'a\nb\tc\u00e9"d"/'
    assert secret_scan.unescape(r"x\\\\ny") == "x\ny"  # three levels of JSON
    assert secret_scan.unescape("q=a%20b%2Fc%0A") == "q=a b/c\n"
    assert secret_scan.unescape("%5Cn") == "\n"  # a JSON escape inside a URL
    assert secret_scan.unescape(r"lone \ and 100% sure") == r"lone \ and 100% sure"


def test_each_occurrence_is_reported_once_across_both_views() -> None:
    for name in ("google api key", "google AQ key"):
        secret = PLANTED[name]
        line = json.dumps({"a": secret, "b": "x\n" + secret})
        assert [h.pattern for h in secret_scan.scan_text(line, "f")] == [name, name]


def test_a_clean_tree_passes_and_a_missing_path_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "trace.jsonl").write_text('{"task": "sk-", "AQ.": 1, "api_key": null}\n')
    assert secret_scan.main([str(tmp_path)]) == 0
    assert secret_scan.main([str(tmp_path / "typo")]) == 2
    assert "not found" in capsys.readouterr().err


def test_the_retained_tree_and_every_cassette_folder_hold_no_key() -> None:
    targets = secret_scan.default_targets(REPO_ROOT)
    assert ACCEPTANCE in targets
    assert REPO_ROOT / "fixtures" / "cassettes" in targets
    scanned, hits = secret_scan.scan(targets)
    assert scanned > 100
    assert hits == [], "\n".join(str(h) for h in hits)


# --- the publish job ----------------------------------------------------------


def _publish_job() -> str:
    workflow = (REPO_ROOT / ".github" / "workflows" / "integration-ci.yml").read_text("utf-8")
    start = workflow.index("\n  publish-results:\n")
    rest = workflow[start + 1 :]
    following = list(re.finditer(r"^  [a-z][a-z-]*:\n", rest, re.M))[1:]
    return rest[: following[0].start()] if following else rest


def test_publish_job_runs_on_main_after_both_gates_and_skips_without_secrets() -> None:
    job = _publish_job()
    assert "    needs: [backend, dashboard]\n" in job
    assert "    if: github.ref == 'refs/heads/main' && github.event_name == 'push'\n" in job
    assert (
        "PUBLISH: ${{ secrets.TRACE_SUPABASE_URL != '' && "
        "secrets.TRACE_SUPABASE_SERVICE_KEY != '' }}" in job
    )
    steps = job.split("\n      - ")[1:]
    assert "if: env.PUBLISH != 'true'" in steps[0], "the first step reports the skip"
    for step in steps[1:]:
        assert "if: env.PUBLISH == 'true'" in step, step
        assert "secrets." not in step, "a secret read outside the job env"
    scan = next(i for i, step in enumerate(steps) if "public_results.secret_scan" in step)
    push = next(i for i, step in enumerate(steps) if "public_results.upload" in step)
    assert scan < push, "the key scan must run before the upload"
    assert "public_results.upload docs/acceptance --prune" in steps[push]
    assert "stale == 'false'" in steps[push] and "stale == 'false'" in steps[scan]
