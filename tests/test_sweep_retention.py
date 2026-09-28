"""Retaining failing sweep cells (#198): replay from cassette, secret scan, the gate.

The first test is permanent. It replays every cell ever retained under
docs/acceptance/runs/live-sweep-* from its own cassette and needs no key, SDK
or network. The rest retain the fake two-provider sweep of tests/test_sweep.py
into a temporary root.
"""

from __future__ import annotations

import errno
import json
import socket
from datetime import UTC, datetime
from pathlib import Path

import pytest

from conftest import REPO_ROOT
from sweep_fakes import FAKE_KEY, FakeGemini, FakeOpenAI, install_fakes, write_suite_and_spec
from trace_harness.cli import main
from trace_harness.runner import sweep_retention
from trace_harness.runner.collector import collect_regressions
from trace_harness.runner.sweep import load_sweep, run_sweep, sweep_dir
from trace_harness.runner.sweep_retention import (
    RETAIN_ROOT,
    RetentionError,
    replay_retained,
    retain_failing_cells,
    retained_cells,
)
from trace_harness.runner.sweep_summary import SweepSummary
from trace_harness.secret_scan import files_under, scan_paths
from trace_harness.tracing.artifact_store import ArtifactStore

RETAINED = retained_cells(REPO_ROOT / RETAIN_ROOT)


@pytest.mark.parametrize(
    ("folder", "run_id"), RETAINED, ids=[f"{folder.name}/{run_id}" for folder, run_id in RETAINED]
)
def test_every_retained_sweep_cell_replays_from_its_cassette(
    folder: Path, run_id: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(REPO_ROOT)
    _forbid_calls(monkeypatch)
    assert replay_retained(folder, run_id, ArtifactStore(tmp_path)).matches


def _forbid_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args, **kwargs):
        pytest.fail("a retained cell tried to reach a provider")

    for name in ("socket", "create_connection", "getaddrinfo"):
        monkeypatch.setattr(socket, name, forbidden)
    for key in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(FakeGemini, "__init__", forbidden)
    monkeypatch.setattr(FakeOpenAI, "__init__", forbidden)


@pytest.fixture
def swept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[ArtifactStore, SweepSummary]:
    install_fakes(monkeypatch)
    # Retention searches for the key values set in the environment.
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    store = ArtifactStore(tmp_path / "runs")
    return store, run_sweep(load_sweep(write_suite_and_spec(tmp_path)), store)


def test_failing_cells_are_retained_and_replay_offline(swept, tmp_path, monkeypatch) -> None:
    store, summary = swept
    root = tmp_path / "acceptance"
    folder = retain_failing_cells(store, summary.sweep_id, root)

    suffix = summary.sweep_id.rsplit("_", 1)[-1]
    assert folder == root / f"live-sweep-{summary.started_at:%Y-%m-%d}-{suffix}"
    assert [p.name for p in root.iterdir()] == [folder.name]
    runs = sorted(p.name for p in folder.glob("run_*"))
    assert runs == sorted(cell.run_id for cell in summary.failing_cells)
    cassettes = sorted(
        p.relative_to(folder).as_posix() for p in folder.glob("cassettes/**/*.jsonl")
    )
    assert cassettes == sorted(cell.cassette_path for cell in summary.failing_cells)
    retained = SweepSummary.model_validate_json((folder / "sweep_summary.json").read_text())
    assert retained == summary
    index = json.loads((folder / "index.json").read_text())
    assert sorted(e["run_id"] for e in index["entries"]) == runs

    _forbid_calls(monkeypatch)
    cells = retained_cells(root)
    assert [run_id for _, run_id in cells] == runs
    for retained_folder, run_id in cells:
        replay = replay_retained(retained_folder, run_id, ArtifactStore(tmp_path / "replay"))
        assert replay.matches
        assert replay.replayed_verdict == "fail"


def test_the_regression_gate_collects_retained_cells(swept, tmp_path) -> None:
    store, summary = swept
    folder = retain_failing_cells(store, summary.sweep_id, tmp_path / "acceptance")
    gate = collect_regressions(folder, ArtifactStore(tmp_path / "gate"))
    assert gate.artifacts_found == len(summary.failing_cells) == 7
    assert gate.blocking == gate.reproduced == 7
    assert gate.exit_code == 0


def test_the_readme_has_a_triage_row_per_cell(swept, tmp_path) -> None:
    store, summary = swept
    readme = (retain_failing_cells(store, summary.sweep_id, tmp_path) / "README.md").read_text()
    rows = [line for line in readme.splitlines() if line.startswith("| `run_")]
    assert len(rows) == 7
    for cell in summary.failing_cells:
        row = next(r for r in rows if cell.run_id in r)
        assert all(f"`{check}`" in row for check in cell.failed_check_ids)
        assert f"| {cell.label} |" in row
        assert row.endswith("| Pending triage. |")
    assert "7 failed, 7 of them verified failures and 2 of those natural" in readme
    assert "GEMINI_API_KEY, ANTHROPIC_API_KEY and OPENAI_API_KEY" in readme


@pytest.mark.parametrize(
    ("leak", "kind"),
    [
        (FAKE_KEY, "value of OPENAI_API_KEY"),
        ("AIza" + "x" * 35, "Google API key"),
        ("AQ." + "x" * 24, "Google AQ. key"),
        ("sk-ant-" + "x" * 24, "Anthropic key"),
        ('{"Authorization": "redacted"}', "auth header field"),
        ('{"x-goog-api-key": "redacted"}', "auth header field"),
        ('{"api_key": "redacted"}', "auth header field"),
        ("Bearer abcdefghijklmnopqrstuvwxyz", "bearer token"),
        # Behind the escapes a JSON string or a URL puts in front of a key.
        (json.dumps("line\n" + "AQ." + "x" * 24), "Google AQ. key"),
        (json.dumps("line\t" + "sk-ant-" + "x" * 24), "Anthropic key"),
        ("q=hello%20" + "sk-proj-" + "x" * 24, "OpenAI key"),
        (json.dumps("line\nBearer abcdefghijklmnopqrstuvwxyz"), "bearer token"),
        (json.dumps(json.dumps({"x-goog-api-key": "redacted"})), "auth header field"),
        (json.dumps("line\n" + FAKE_KEY), "value of OPENAI_API_KEY"),
    ],
)
def test_a_secret_stops_retention_and_is_never_echoed(swept, tmp_path, leak, kind) -> None:
    store, summary = swept
    leaked = store.run_dir(summary.failing_cells[0].run_id) / "final_state.json"
    leaked.write_text(leaked.read_text() + leak)
    root = tmp_path / "acceptance"
    with pytest.raises(RetentionError, match=f"final_state.json:\\d+: {kind}") as error:
        retain_failing_cells(store, summary.sweep_id, root)
    assert FAKE_KEY not in str(error.value)
    assert not root.exists()


def test_a_local_path_stops_retention(swept, tmp_path) -> None:
    store, summary = swept
    leaked = store.run_dir(summary.failing_cells[0].run_id) / "final_state.json"
    leaked.write_text(leaked.read_text() + str(store.runs_dir.resolve() / "elsewhere"))
    with pytest.raises(RetentionError, match="final_state.json:\\d+: local path"):
        retain_failing_cells(store, summary.sweep_id, tmp_path / "acceptance")


def test_retained_run_configs_name_no_local_path(swept, tmp_path) -> None:
    """The sweep recorded its cassette paths under an absolute runs directory."""
    store, summary = swept
    assert store.runs_dir.is_absolute()
    folder = retain_failing_cells(store, summary.sweep_id, tmp_path / "acceptance")
    for path in files_under([folder]):
        assert str(tmp_path) not in path.read_text(encoding="utf-8"), path
    for cell in summary.failing_cells:
        config = json.loads((folder / cell.run_id / "run_config.json").read_text())
        assert config["cassette"] == {"mode": "record", "directory": "cassettes"}
        assert config["metadata"]["cassette_path"] == cell.cassette_path
        assert (folder / cell.cassette_path).is_file()


def test_the_copy_is_assembled_where_no_gate_looks(swept, tmp_path, monkeypatch) -> None:
    """Nothing, hidden or not, appears under the retain root before the checks pass."""
    store, summary = swept
    root = tmp_path / "acceptance"
    root.mkdir()
    seen = []

    def watching(targets, **kwargs):
        seen.append(sorted(p.name for p in root.iterdir()))
        assert targets[0].is_relative_to(store.runs_dir)
        return scan_paths(targets, **kwargs)

    monkeypatch.setattr(sweep_retention, "scan_paths", watching)
    folder = retain_failing_cells(store, summary.sweep_id, root)
    assert seen == [[]]
    assert [p.name for p in root.iterdir()] == [folder.name]
    assert not list(store.runs_dir.glob("sweeps/*/retaining-*"))


def test_a_runs_dir_on_another_filesystem_still_lands_whole(swept, tmp_path, monkeypatch):
    store, summary = swept
    rename = Path.rename

    def across_devices(self, target):
        if self.is_relative_to(store.runs_dir):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return rename(self, target)

    monkeypatch.setattr(Path, "rename", across_devices)
    root = tmp_path / "acceptance"
    folder = retain_failing_cells(store, summary.sweep_id, root)
    assert [p.name for p in root.iterdir()] == [folder.name]
    assert len(retained_cells(root)) == len(summary.failing_cells)
    assert not list(store.runs_dir.glob("sweeps/*/retaining-*"))


def test_two_sweeps_on_one_day_retain_into_two_folders(tmp_path, monkeypatch) -> None:
    install_fakes(monkeypatch)
    # Both start at one instant, so only the sweep id can tell them apart.
    start = datetime(2026, 9, 24, 23, 59, 59, tzinfo=UTC)
    monkeypatch.setattr("trace_harness.runner.sweep.utc_now", lambda: start)
    store, root = ArtifactStore(tmp_path / "runs"), tmp_path / "acceptance"
    spec = load_sweep(write_suite_and_spec(tmp_path))
    first, second = run_sweep(spec, store), run_sweep(spec, store)
    assert first.started_at == second.started_at
    assert first.sweep_id != second.sweep_id
    folders = {retain_failing_cells(store, s.sweep_id, root) for s in (first, second)}
    assert len(folders) == 2
    assert sorted(p.name for p in root.iterdir()) == sorted(f.name for f in folders)


def test_a_cell_that_does_not_replay_is_refused(swept, tmp_path) -> None:
    store, summary = swept
    cell = summary.failing_cells[0]
    path = store.run_dir(cell.run_id) / "verifier_result.json"
    result = json.loads(path.read_text())
    result["failed_checks"][0]["check_id"] = "a_check_that_never_fired"
    path.write_text(json.dumps(result))
    root = tmp_path / "acceptance"
    with pytest.raises(RetentionError, match=f"{cell.run_id} does not replay"):
        retain_failing_cells(store, summary.sweep_id, root)
    assert not root.exists()


def test_an_existing_folder_is_never_overwritten(swept, tmp_path) -> None:
    store, summary = swept
    folder = retain_failing_cells(store, summary.sweep_id, tmp_path)
    before = sorted(p.name for p in folder.iterdir())
    with pytest.raises(RetentionError, match="already exists"):
        retain_failing_cells(store, summary.sweep_id, tmp_path)
    assert sorted(p.name for p in folder.iterdir()) == before


def test_the_committed_evidence_scans_clean() -> None:
    """The shapes find nothing in any artifact already retained, escaped or not."""
    targets = [
        REPO_ROOT / root
        for root in ("docs/acceptance", "fixtures/cassettes", "fixtures/controls/evidence")
    ]
    assert len(files_under(targets)) > 150
    hits = scan_paths(targets, relative_to=REPO_ROOT)
    assert hits == [], "\n".join(map(str, hits))


def test_run_sweep_retains_and_retain_sweep_refuses_a_second_copy(
    tmp_path, monkeypatch, capsys
) -> None:
    install_fakes(monkeypatch)
    runs, root = tmp_path / "runs", tmp_path / "acceptance"
    spec = str(write_suite_and_spec(tmp_path))
    assert main(["run-sweep", spec, "--runs-dir", str(runs), "--retain", str(root)]) == 0
    assert "retained:" in capsys.readouterr().out
    [folder] = root.iterdir()
    assert len(list(folder.glob("run_*"))) == 7
    [sweep] = (runs / "sweeps").iterdir()
    assert main(["retain-sweep", sweep.name, "--runs-dir", str(runs), "--to", str(root)]) == 2


def test_a_refused_retention_exits_2_after_the_sweep_is_written(
    tmp_path, monkeypatch, capsys
) -> None:
    install_fakes(monkeypatch)
    # A "key" every retained run config holds, so the scan refuses.
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-3.6-flash")
    runs, root = tmp_path / "runs", tmp_path / "acceptance"
    spec = str(write_suite_and_spec(tmp_path))
    assert main(["run-sweep", spec, "--runs-dir", str(runs), "--retain", str(root)]) == 2
    assert "value of GEMINI_API_KEY" in capsys.readouterr().err
    assert not root.exists()
    [summary] = (runs / "sweeps").glob("*/sweep_summary.json")
    assert len(SweepSummary.model_validate_json(summary.read_text()).failing_cells) == 7


def test_a_sweep_without_failures_retains_nothing(tmp_path, monkeypatch) -> None:
    install_fakes(monkeypatch)
    spec = write_suite_and_spec(tmp_path, seeds=[2])
    data = json.loads(spec.read_text())
    data["providers"] = data["providers"][:1]
    spec.write_text(json.dumps(data))
    store = ArtifactStore(tmp_path / "runs")
    summary = run_sweep(load_sweep(spec), store)
    assert summary.failing_cells == []
    assert retain_failing_cells(store, summary.sweep_id, tmp_path / "acceptance") is None
    assert not (tmp_path / "acceptance").exists()
    assert (sweep_dir(store.runs_dir, summary.sweep_id) / "sweep_summary.json").is_file()
