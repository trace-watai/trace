"""Cassette contracts: strict offline replay, privacy, and real task outcomes."""

from __future__ import annotations

import json
import runpy
import socket
import sys
from pathlib import Path

import pytest

from conftest import FAILURE_TASK_PATH, FIXTURES_DIR, REPO_ROOT, VALID_TASK_PATH
from trace_harness.cli import main
from trace_harness.models import create_model_adapter
from trace_harness.models.base import AgentAction, Message, ModelAdapter, ToolSpec
from trace_harness.models.cassette import (
    CassetteConfig,
    CassetteError,
    CassetteRequestConfig,
    RecordingModelAdapter,
)
from trace_harness.models.fixture import FixtureModelAdapter
from trace_harness.models.gemini import GeminiModelAdapter
from trace_harness.runner.batch import BatchRunner
from trace_harness.runner.pipeline import run_task_pipeline
from trace_harness.runner.suite import AgentConfig, load_suite
from trace_harness.tracing.artifact_store import ArtifactStore
from trace_harness.verifiers.base import VerifierResult

LIVE_RUN = REPO_ROOT / "docs/acceptance/live-gemini-2026-09-13/run_20260913T141412Z_3e4b44a9"
LIVE_CASSETTE = FIXTURES_DIR / "cassettes/refund_policy_failure/gemini-3.6-flash/default.jsonl"
CONFIG = CassetteRequestConfig(task_id="unit", provider="gemini", model="test-model", seed=7)
TRANSCRIPT = [Message(role="user", content="Resolve this request.")]
TOOLS = [ToolSpec(name="lookup", description="Read the order", parameters={"type": "object"})]


class StubAdapter:
    name = "gemini"
    api_key = "test-credential-must-never-be-written"

    def __init__(self, action: AgentAction | None = None) -> None:
        self.calls = 0
        self.action = action or AgentAction(
            kind="final_answer",
            final_answer="Done.",
            provider_state={"thought_signature": "opaque-signature"},
            raw={
                "sdk_http_response": {"headers": {"Authorization": self.api_key}},
                "api_key": self.api_key,
                "usage_metadata": {"total_token_count": 12, "headers": self.api_key},
            },
        )

    def next_action(self, transcript, tools):
        self.calls += 1
        return self.action


def record(path: Path, inner: StubAdapter | None = None) -> AgentAction:
    adapter = RecordingModelAdapter(
        mode="record", path=path, config=CONFIG, inner=inner or StubAdapter()
    )
    assert isinstance(adapter, ModelAdapter)
    return adapter.next_action(TRANSCRIPT, TOOLS)


def replay(path: Path, config: CassetteRequestConfig = CONFIG) -> RecordingModelAdapter:
    adapter = RecordingModelAdapter(mode="replay", path=path, config=config)
    assert isinstance(adapter, ModelAdapter)
    return adapter


def forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args, **kwargs):
        pytest.fail("replay attempted to construct a provider or open a socket")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(GeminiModelAdapter, "__init__", forbidden)
    monkeypatch.setattr(FixtureModelAdapter, "from_file", forbidden)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setitem(sys.modules, "google.genai", None)


def normalized_trace_bytes(path: Path) -> bytes:
    # Keep audit IDs/timestamps real, as in test_fixture_run's existing contract.
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    for row in rows:
        del row["run_id"], row["timestamp"]
    return ("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n").encode()


def test_record_and_replay_return_same_safe_action(tmp_path: Path) -> None:
    path = tmp_path / "cassette.jsonl"
    stub = StubAdapter()
    actual = record(path, stub)
    assert stub.calls == 1
    assert actual == replay(path).next_action(TRANSCRIPT, TOOLS)
    assert actual.raw == {"usage_metadata": {"total_token_count": 12}}
    assert actual.provider_state == stub.action.provider_state
    assert stub.action.raw["api_key"] == stub.api_key  # wrapper did not mutate its inner response
    assert stub.api_key not in path.read_text()
    assert "headers" not in path.read_text()
    assert "sdk_http_response" not in path.read_text()
    assert TRANSCRIPT[0].content not in path.read_text()


@pytest.mark.parametrize("change", ["prompt", "observation", "signature", "tools"])
def test_request_miss_is_hard_error_and_does_not_advance(tmp_path: Path, change: str) -> None:
    path = tmp_path / "cassette.jsonl"
    record(path)
    transcript = [m.model_copy(deep=True) for m in TRANSCRIPT]
    tools = [t.model_copy(deep=True) for t in TOOLS]
    if change == "prompt":
        transcript[0].content += "changed"
    elif change == "observation":
        transcript.append(Message(role="tool", content="", metadata={"result": "changed"}))
    elif change == "signature":
        transcript[0].metadata["provider_state"] = {"thought_signature": "changed"}
    else:
        tools[0].parameters["required"] = ["order_id"]
    adapter = replay(path)
    with pytest.raises(CassetteError, match="request mismatch at step 1"):
        adapter.next_action(transcript, tools)
    assert adapter.next_action(TRANSCRIPT, TOOLS).final_answer == "Done."
    with pytest.raises(CassetteError, match="exhausted at step 2"):
        adapter.next_action(TRANSCRIPT, TOOLS)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("task_id", "another-task"),
        ("provider", "fixture"),
        ("model", "another-model"),
        ("temperature", 0.1),
        ("seed", 8),
        ("timeout_seconds", 60.0),
        ("prompt_version", "v1"),
    ],
)
def test_configuration_drift_is_rejected(tmp_path: Path, field: str, value: object) -> None:
    path = tmp_path / "cassette.jsonl"
    record(path)
    with pytest.raises(CassetteError, match="configuration mismatch"):
        replay(path, CONFIG.model_copy(update={field: value}))


@pytest.mark.parametrize(
    "damage",
    ["version", "json", "trailing_json", "step", "raw", "nested_extra", "usage", "empty"],
)
def test_invalid_cassette_rejected_before_any_action(tmp_path: Path, damage: str) -> None:
    path = tmp_path / "cassette.jsonl"
    record(path)
    data = json.loads(path.read_text())
    if damage == "version":
        data["schema_version"] = "99.0"
    elif damage == "step":
        data["step"] = 2
    elif damage == "raw":
        data["response"]["raw"] = {"headers": "sensitive"}
    elif damage == "nested_extra":
        data["response"]["kind"] = "tool_call"
        data["response"]["tool_call"] = {"tool_name": "lookup", "arguments": {}, "typo": 1}
    elif damage == "usage":
        data["usage"]["total_token_count"] = True
    text = json.dumps(data) + "\n"
    if damage == "json":
        text = '{"response":'
    elif damage == "trailing_json":
        text += '{"response":'
    elif damage == "empty":
        text = ""
    path.write_text(text)
    with pytest.raises(CassetteError):
        replay(path)


def test_missing_file_and_overwrite_are_errors(tmp_path: Path) -> None:
    path = tmp_path / "cassette.jsonl"
    with pytest.raises(CassetteError, match="not found"):
        replay(path)
    record(path)
    before = path.read_bytes()
    stub = StubAdapter()
    with pytest.raises(CassetteError, match="already exists"):
        record(path, stub)
    assert path.read_bytes() == before
    assert stub.calls == 0


@pytest.mark.parametrize("location", ["reasoning", "provider_state"])
def test_credential_in_action_fails_before_writing(tmp_path: Path, location: str) -> None:
    action = AgentAction(kind="final_answer", final_answer="done")
    if location == "reasoning":
        action.reasoning = StubAdapter.api_key
    else:
        action.provider_state = {"authorization": "another-secret"}
    path = tmp_path / "cassette.jsonl"
    with pytest.raises(CassetteError, match="credential") as error:
        record(path, StubAdapter(action))
    assert "secret" not in str(error.value)
    assert path.read_bytes() == b""


def test_provider_error_is_not_persisted(tmp_path: Path) -> None:
    class Broken(StubAdapter):
        def next_action(self, transcript, tools):
            raise RuntimeError(self.api_key)

    path = tmp_path / "cassette.jsonl"
    with pytest.raises(CassetteError, match="recording model call failed") as error:
        record(path, Broken())
    assert Broken.api_key not in str(error.value)
    assert path.read_bytes() == b""


def test_replay_forbids_an_inner_adapter(tmp_path: Path) -> None:
    with pytest.raises(CassetteError, match="must not have an inner"):
        RecordingModelAdapter(
            mode="replay", path=tmp_path / "x", config=CONFIG, inner=StubAdapter()
        )


def test_fixture_is_still_default_and_mode_never_comes_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("TRACE_CASSETTE_MODE", "record")
    adapter = create_model_adapter(
        "fixture", script_path=FIXTURES_DIR / "scripts/refund_policy_failure_script.json"
    )
    assert isinstance(adapter, FixtureModelAdapter)
    assert AgentConfig(label="default").cassette is None


def test_entire_suite_replays_offline_with_identical_outcomes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(REPO_ROOT)
    suite = load_suite(FIXTURES_DIR / "suites/refund_v0.json")
    directory = str(tmp_path / "cassettes")
    suite.agent_configs[0].cassette = CassetteConfig(mode="record", directory=directory)
    recorded_store = ArtifactStore(tmp_path / "recorded")
    recorded = BatchRunner(recorded_store).run(suite)
    assert recorded.aggregates.completed == 32
    assert recorded.aggregates.verifier_passed == 18
    assert recorded.aggregates.verifier_failed == 14

    forbid_network(monkeypatch)
    suite.agent_configs[0].cassette = CassetteConfig(mode="replay", directory=directory)
    for repeat in range(2):
        store = ArtifactStore(tmp_path / f"replay-{repeat}")
        replayed = BatchRunner(store).run(suite)
        assert replayed.aggregates.completed == 32
        assert replayed.aggregates.errored == 0
        for original, actual in zip(recorded.entries, replayed.entries, strict=True):
            assert original.verifier_passed == actual.verifier_passed
            assert normalized_trace_bytes(recorded_store.trace_path(original.run_id)) == (
                normalized_trace_bytes(store.trace_path(actual.run_id))
            )
            before = recorded_store.read_json(original.run_id, "verifier_result.json")
            after = store.read_json(actual.run_id, "verifier_result.json")
            before.pop("run_id")
            after.pop("run_id")
            assert before == after


def test_retained_live_gemini_replay_matches_original_verdict_and_state(
    tmp_path, monkeypatch
) -> None:
    forbid_network(monkeypatch)
    config = AgentConfig(
        label="retained-gemini",
        provider="gemini",
        cassette=CassetteConfig(mode="replay", directory=str(FIXTURES_DIR / "cassettes")),
    )
    original = VerifierResult.model_validate_json((LIVE_RUN / "verifier_result.json").read_text())
    original_verdict = original.model_dump(exclude={"run_id", "schema_version"})
    trace_bytes = []
    for repeat in range(2):
        store = ArtifactStore(tmp_path / f"replay-{repeat}")
        actual = run_task_pipeline(FAILURE_TASK_PATH, config, store)
        assert actual.run_result.status == "completed"
        assert actual.run_result.steps_taken == 5
        assert actual.run_config.model == "gemini-3.6-flash"
        assert (
            actual.verifier_result.model_dump(exclude={"run_id", "schema_version"})
            == original_verdict
        )
        assert store.read_json(actual.run_result.run_id, "final_state.json") == json.loads(
            (LIVE_RUN / "final_state.json").read_text()
        )
        trace_bytes.append(normalized_trace_bytes(store.trace_path(actual.run_result.run_id)))
    assert trace_bytes[0] == trace_bytes[1]


def test_retained_import_is_reproducible_and_never_overwrites(tmp_path: Path) -> None:
    importer = runpy.run_path(str(REPO_ROOT / "scripts/import_model_cassette.py"))[
        "import_cassette"
    ]
    path = importer(LIVE_RUN, tmp_path)
    assert path.read_bytes() == LIVE_CASSETTE.read_bytes()
    with pytest.raises(FileExistsError):
        importer(LIVE_RUN, tmp_path)


def test_cli_records_and_replays_same_knobs_without_keys(tmp_path, monkeypatch) -> None:
    captured = {}

    def stub_init(self, model, *, temperature, seed, timeout_seconds):
        captured.update(
            model=model, temperature=temperature, seed=seed, timeout_seconds=timeout_seconds
        )

    monkeypatch.setattr(GeminiModelAdapter, "__init__", stub_init)
    monkeypatch.setattr(GeminiModelAdapter, "next_action", StubAdapter().next_action)
    args = [
        "run-fixture",
        str(VALID_TASK_PATH),
        "--provider",
        "gemini",
        "--temperature",
        "0.25",
        "--seed",
        "9",
        "--timeout",
        "17",
        "--cassette-dir",
        str(tmp_path / "cassettes"),
    ]
    runs = tmp_path / "recorded"
    assert main([*args, "--cassette-mode", "record", "--runs-dir", str(runs)]) == 0
    assert captured == {
        "model": "gemini-3.6-flash",
        "temperature": 0.25,
        "seed": 9,
        "timeout_seconds": 17.0,
    }
    saved = json.loads(next(runs.glob("*/run_config.json")).read_text())
    assert {key: saved[key] for key in captured} == captured
    assert saved["cassette"]["mode"] == "record"
    assert saved["metadata"]["cassette_path"].endswith("gemini-3.6-flash/9.jsonl")
    forbid_network(monkeypatch)
    assert main([*args, "--cassette-mode", "replay", "--runs-dir", str(tmp_path / "replayed")]) == 0
    path = next((tmp_path / "cassettes").rglob("*.jsonl"))
    data = json.loads(path.read_text())
    data["transcript_hash"] = "0" * 64
    path.write_text(json.dumps(data) + "\n")
    assert main([*args, "--cassette-mode", "replay", "--runs-dir", str(tmp_path / "miss")]) == 2
    assert main([*args, "--runs-dir", str(tmp_path / "no-mode")]) == 2


def test_cli_replay_suite_and_missing_cassette_exit_codes(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(REPO_ROOT)
    forbid_network(monkeypatch)
    manifest = FIXTURES_DIR / "suites/refund_policy_gemini_replay.json"
    assert main(["run-suite", str(manifest), "--runs-dir", str(tmp_path / "success")]) == 0
    suite = json.loads(manifest.read_text())
    suite["agent_configs"][0]["cassette"]["directory"] = str(tmp_path / "missing")
    broken = tmp_path / "broken-suite.json"
    broken.write_text(json.dumps(suite))
    assert main(["run-suite", str(broken), "--runs-dir", str(tmp_path / "errors")]) == 2
