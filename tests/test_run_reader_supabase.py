"""SupabaseRunReader against synthesized PostgREST responses, with no network.

The retained tree under docs/acceptance/ is staged and read through the
filesystem RunReader, turned into rows, and served back through PostgREST
stand-ins (tests/postgrest_fake.py). The Supabase reader must then return
exactly what the filesystem reader returns, method by method, for every
retained run, batch and experiment. The one exception is a suite report that
was never persisted, whose generated_at the filesystem reader stamps with the
current time on every call.

The committed fixture tests/fixtures/postgrest/synthesized_retained.json is
replayed like a cassette, so the reader's requests are pinned too.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

import pytest

import postgrest_fake as fake
from trace_harness.cli import main
from trace_harness.public_results import schema
from trace_harness.public_results.postgrest import (
    HttpRequest,
    HttpResponse,
    PostgrestClient,
    PostgrestError,
    urllib_transport,
)
from trace_harness.run_reader import RunNotFound, RunReader
from trace_harness.run_reader_supabase import SupabaseRunReader
from trace_harness.run_readers import open_run_reader, reader_location
from trace_harness.tracing.artifact_store import ArtifactStore


@pytest.fixture(scope="module")
def retained(tmp_path_factory: pytest.TempPathFactory) -> tuple[RunReader, dict[str, list[dict]]]:
    return fake.retained_rows(tmp_path_factory.mktemp("retained") / "staged")


def supabase_reader(server: Any, *, key: str = fake.ANON_KEY, page_size: int = 1000):
    return SupabaseRunReader(
        PostgrestClient(fake.BASE_URL, key, transport=server, page_size=page_size)
    )


# --- the interface ------------------------------------------------------------


def _public_methods(cls: type) -> dict[str, inspect.Signature]:
    return {
        name: inspect.signature(member)
        for name, member in inspect.getmembers(cls, inspect.isfunction)
        if not name.startswith("_")
    }


def test_supabase_reader_has_every_filesystem_read_with_the_same_signature() -> None:
    filesystem = _public_methods(RunReader)
    hosted = _public_methods(SupabaseRunReader)
    assert set(filesystem) <= set(hosted), "RunReader gained a read the Supabase backend lacks"
    for name, signature in filesystem.items():
        assert str(hosted[name]) == str(signature), name


# --- parity -------------------------------------------------------------------


def test_the_retained_set_is_what_the_issue_expects(retained) -> None:
    fs, rows = retained
    acceptance = fake.REPO_ROOT / "docs" / "acceptance"
    run_dirs = len(list(acceptance.rglob("run_result.json")))
    assert len(rows[schema.RUNS]) == len(fs.list_runs()) == run_dirs >= 15
    assert len(rows[schema.BATCHES]) >= 2
    assert len(rows[schema.EXPERIMENTS]) == len(list(acceptance.rglob("experiment.json"))) >= 1
    full_chain = [r for r in rows[schema.RUNS] if r["failure_card"] is not None]
    assert full_chain, "no retained run carries a bundle"


@pytest.mark.parametrize("page_size", [1000, 4])
def test_supabase_reader_returns_what_the_filesystem_reader_returns(retained, page_size) -> None:
    fs, rows = retained
    server = fake.MemoryPostgrest().load(rows)
    hosted = supabase_reader(server, page_size=page_size)
    fake.assert_same_reads(fs, hosted, [r["batch_id"] for r in rows[schema.BATCHES]])
    assert not server.writes()


def test_a_reproduction_reads_the_bundle_of_the_run_holding_its_card(tmp_path: Path) -> None:
    root = fake.retained_with_reproduction(tmp_path / "retained")
    fs, staged, rows = fake.reproduction_rows(root, tmp_path / "staged")
    server = fake.MemoryPostgrest().load(rows)
    hosted = supabase_reader(server)

    fake.assert_same_reads(fs, hosted, [], staged.bundle_refs)
    assert hosted.get_bundle(fake.REPRODUCTION_RUN) == fs.get_bundle(fake.CARD_RUN) is not None

    # A pointer whose card row is gone reads like a card file that is gone.
    del server.tables[schema.RUNS][fake.CARD_RUN]
    with pytest.raises(FileNotFoundError, match=f"failure card of run '{fake.CARD_RUN}'"):
        hosted.get_bundle(fake.REPRODUCTION_RUN)


def test_committed_fixture_replays_to_the_filesystem_answers(retained) -> None:
    fs, _ = retained
    fixture = json.loads(fake.FIXTURE_PATH.read_text(encoding="utf-8"))
    assert "Synthesized PostgREST responses" in fixture["note"]
    replay = fake.ReplayTransport(fixture)
    hosted = SupabaseRunReader(
        PostgrestClient(
            fixture["base_url"], fake.ANON_KEY, transport=replay, page_size=fake.FIXTURE_PAGE_SIZE
        )
    )

    assert hosted.list_runs() == fs.list_runs()
    assert hosted.list_runs_for_batch(fake.MEMBER_BATCH) == fs.list_runs_for_batch(
        fake.MEMBER_BATCH
    )
    assert hosted.list_runs_for_batch("batch_not_retained") == []
    for method in ("get_run", "get_task", "get_trace", "get_verifier", "get_attribution"):
        assert getattr(hosted, method)(fake.FULL_CHAIN_RUN) == getattr(fs, method)(
            fake.FULL_CHAIN_RUN
        )
    assert hosted.get_bundle(fake.FULL_CHAIN_RUN) == fs.get_bundle(fake.FULL_CHAIN_RUN)
    assert hosted.get_verifier(fake.PASSING_RUN) == fs.get_verifier(fake.PASSING_RUN)
    assert hosted.get_attribution(fake.PASSING_RUN) is None
    assert hosted.get_bundle(fake.PASSING_RUN) is None
    assert hosted.get_batch_summary(fake.FIXTURE_BATCH) == fs.get_batch_summary(fake.FIXTURE_BATCH)
    assert fake.without_generated_at(hosted.get_suite_report(fake.FIXTURE_BATCH)) == (
        fake.without_generated_at(fs.get_suite_report(fake.FIXTURE_BATCH))
    )
    assert hosted.list_experiments() == fs.list_experiments()
    assert hosted.get_experiment(fake.FIXTURE_EXPERIMENT) == fs.get_experiment(
        fake.FIXTURE_EXPERIMENT
    )
    with pytest.raises(RunNotFound):
        hosted.get_run("run_not_retained")
    with pytest.raises(FileNotFoundError):
        hosted.get_batch_summary("batch_not_retained")
    with pytest.raises(FileNotFoundError):
        hosted.get_experiment("exp_not_retained")
    unused = set(replay.exchanges) - replay.used
    assert len(unused) == 2  # the two refusals, exercised below


def test_committed_fixture_is_shaped_like_postgrest_output() -> None:
    fixture = json.loads(fake.FIXTURE_PATH.read_text(encoding="utf-8"))
    for exchange in fixture["exchanges"]:
        request, response = exchange["request"], exchange["response"]
        assert request["url"].startswith(fake.BASE_URL + "/rest/v1/")
        if response["status"] in (200, 206):
            assert isinstance(response["body"], list)
            assert all(isinstance(row, dict) for row in response["body"])
            assert "content-range" in response["headers"]
            if "limit=" in request["url"]:
                assert not response["headers"]["content-range"].endswith("/*")
        else:
            assert set(response["body"]) == {"code", "details", "hint", "message"}
    statuses = {e["response"]["status"] for e in fixture["exchanges"]}
    assert {200, 206, 401, 404} <= statuses


def test_committed_fixture_refusals_surface_as_errors_without_the_key() -> None:
    fixture = json.loads(fake.FIXTURE_PATH.read_text(encoding="utf-8"))
    replay = fake.ReplayTransport(fixture)
    wrong = SupabaseRunReader(
        PostgrestClient(fake.BASE_URL, "sb_publishable_WRONG", transport=replay)
    )
    with pytest.raises(PostgrestError) as refused:
        wrong.client.select_one(schema.RUNS, "summary", "run_id", "x")
    assert refused.value.status == 401 and "Invalid API key" in str(refused.value)
    assert "sb_publishable_WRONG" not in str(refused.value)
    anon = PostgrestClient(fake.BASE_URL, fake.ANON_KEY, transport=replay)
    with pytest.raises(PostgrestError) as missing:
        anon._json_rows(anon._send("GET", "runz", [("select", "summary")]), "runz")
    assert missing.value.status == 404 and missing.value.code == "PGRST205"


# --- paging, keys, errors -----------------------------------------------------


def test_paging_follows_content_range_and_a_server_row_cap(retained) -> None:
    fs, rows = retained
    # max_rows below page_size models Supabase's max-rows cap: the server
    # returns fewer rows than asked, and the reader must keep going.
    server = fake.MemoryPostgrest(max_rows=3).load(rows)
    hosted = supabase_reader(server, page_size=5)
    assert hosted.list_runs() == fs.list_runs()
    offsets = [p.offset for p in server.requests if p.table == schema.RUNS]
    assert offsets == list(range(0, len(rows[schema.RUNS]), 3))


def test_paging_without_a_count_stops_on_a_short_page() -> None:
    pages = [[{"summary": i} for i in range(2)], [{"summary": 2}]]
    seen: list[str] = []

    def transport(request: HttpRequest) -> HttpResponse:
        seen.append(request.url)
        body = pages[len(seen) - 1]
        return HttpResponse(206, {"content-range": "0-1/*"}, json.dumps(body).encode())

    client = PostgrestClient(fake.BASE_URL, fake.ANON_KEY, transport=transport, page_size=2)
    assert [r["summary"] for r in client.select(schema.RUNS, "summary")] == [0, 1, 2]
    assert len(seen) == 2


@pytest.mark.parametrize(
    ("key", "bearer"),
    [(fake.ANON_KEY, False), (fake.make_jwt("anon"), True)],
    ids=["publishable", "legacy-jwt"],
)
def test_keys_are_sent_the_way_supabase_expects(retained, key: str, bearer: bool) -> None:
    captured: list[HttpRequest] = []
    server = fake.MemoryPostgrest().load(retained[1])

    def spy(request: HttpRequest) -> HttpResponse:
        captured.append(request)
        return server(request)

    assert supabase_reader(spy, key=key).list_experiments()
    headers = captured[0].headers
    assert headers["apikey"] == key
    assert ("Authorization" in headers) is bearer
    assert headers["Prefer"] == "count=exact"


@pytest.mark.parametrize("role", ["secret", "legacy"])
def test_reader_refuses_a_key_that_bypasses_row_level_security(role: str) -> None:
    key = fake.SERVICE_KEY if role == "secret" else fake.make_jwt("service_role")
    with pytest.raises(ValueError, match="bypasses row level security"):
        supabase_reader(fake.MemoryPostgrest(), key=key)


def test_from_env_names_what_is_missing_and_wants_https() -> None:
    with pytest.raises(ValueError, match="TRACE_SUPABASE_URL and TRACE_SUPABASE_ANON_KEY"):
        SupabaseRunReader.from_env({})
    with pytest.raises(ValueError, match="https://"):
        SupabaseRunReader.from_env(
            {"TRACE_SUPABASE_URL": "http://example.supabase.co", "TRACE_SUPABASE_ANON_KEY": "k"}
        )
    reader = SupabaseRunReader.from_env(
        {"TRACE_SUPABASE_URL": fake.BASE_URL + "/", "TRACE_SUPABASE_ANON_KEY": fake.ANON_KEY},
        transport=fake.MemoryPostgrest(),
    )
    assert reader.location == fake.BASE_URL
    assert fake.ANON_KEY not in repr(reader.client)


def test_a_malformed_body_is_an_error() -> None:
    def transport(request: HttpRequest) -> HttpResponse:
        return HttpResponse(200, {}, b'{"not": "an array"}')

    reader = supabase_reader(transport)
    with pytest.raises(PostgrestError, match="expected a JSON array"):
        reader.get_run("x")


def test_urllib_transport_returns_error_statuses_and_hides_nothing_it_should_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The only code that reaches the network, exercised with urlopen replaced."""
    import io
    import urllib.error
    import urllib.request
    from email.message import Message

    calls: list[urllib.request.Request] = []

    class Ok(io.BytesIO):
        status = 206
        headers = Message()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    Ok.headers["Content-Range"] = "0-0/1"

    def fake_urlopen(request, timeout):  # noqa: ARG001
        calls.append(request)
        if "fail" in request.full_url:
            headers = Message()
            headers["Content-Type"] = "application/json"
            raise urllib.error.HTTPError(
                request.full_url, 401, "Unauthorized", headers, io.BytesIO(b'{"message":"no"}')
            )
        if "down" in request.full_url:
            raise urllib.error.URLError("connection refused")
        return Ok(b"[]")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    ok = urllib_transport(HttpRequest("GET", "https://x.supabase.co/rest/v1/runs", {"apikey": "k"}))
    assert ok.status == 206 and ok.headers["content-range"] == "0-0/1" and ok.body == b"[]"
    assert calls[0].get_header("Apikey") == "k"
    failed = urllib_transport(HttpRequest("GET", "https://x.supabase.co/fail", {}))
    assert failed.status == 401 and failed.body == b'{"message":"no"}'
    with pytest.raises(PostgrestError, match="could not reach x.supabase.co"):
        urllib_transport(HttpRequest("GET", "https://x.supabase.co/down", {}))


# --- choosing the backend -----------------------------------------------------


def test_filesystem_stays_the_default(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    for env in ({}, {"TRACE_RUN_READER": ""}, {"TRACE_RUN_READER": "filesystem"}):
        reader = open_run_reader(store, env)
        assert type(reader) is RunReader
        assert reader_location(reader) == str(tmp_path)


def test_supabase_is_chosen_by_env_and_unknown_values_are_refused(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    env = {
        "TRACE_RUN_READER": "supabase",
        "TRACE_SUPABASE_URL": fake.BASE_URL,
        "TRACE_SUPABASE_ANON_KEY": fake.ANON_KEY,
    }
    assert isinstance(open_run_reader(store, env), SupabaseRunReader)
    with pytest.raises(ValueError, match="TRACE_RUN_READER must be one of"):
        open_run_reader(store, {"TRACE_RUN_READER": "sqlite"})


def test_cli_list_runs_reads_the_hosted_results_when_selected(
    retained, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    fs, rows = retained
    server = fake.MemoryPostgrest().load(rows)
    original = SupabaseRunReader.from_env.__func__

    def from_env_with_fake(cls, env=None, **kwargs):
        return original(cls, env, transport=server)

    monkeypatch.setattr(SupabaseRunReader, "from_env", classmethod(from_env_with_fake))
    monkeypatch.setenv("TRACE_RUN_READER", "supabase")
    monkeypatch.setenv("TRACE_SUPABASE_URL", fake.BASE_URL)
    monkeypatch.setenv("TRACE_SUPABASE_ANON_KEY", fake.ANON_KEY)

    assert main(["--runs-dir", str(tmp_path / "empty"), "list-runs"]) == 0
    out = capsys.readouterr().out
    assert f"{len(fs.list_runs())} run(s) in {fake.BASE_URL}" in out
    assert main(["--runs-dir", str(tmp_path / "empty"), "list-experiments"]) == 0
    experiments = len(fs.list_experiments())
    assert f"{experiments} experiment(s) in {fake.BASE_URL}" in capsys.readouterr().out


def test_cli_rejects_an_unknown_backend(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("TRACE_RUN_READER", "sqlite")
    assert main(["--runs-dir", str(tmp_path), "list-runs"]) == 2
    assert "TRACE_RUN_READER must be one of" in capsys.readouterr().err
