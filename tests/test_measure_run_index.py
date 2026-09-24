"""scripts/measure_run_index.py: its checks and its handling of the work directory.

The script is a measurement, so pytest never runs it at scale. These tests run
it at the smallest scale with the list-runs subprocess stubbed out, and check
that it refuses to time a directory it cannot vouch for.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from conftest import REPO_ROOT
from trace_harness.tracing.artifact_store import ArtifactStore
from trace_harness.tracing.run_index import RunIndex, RunIndexEntry

SCRIPT = REPO_ROOT / "scripts" / "measure_run_index.py"


@pytest.fixture(scope="module")
def mri() -> ModuleType:
    name = "measure_run_index_under_test"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses look their module up here
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(name, None)


@pytest.fixture
def cli_calls(mri: ModuleType, monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Stub the list-runs subprocess timing and record which dirs it was asked to time."""
    calls: list[Path] = []

    def fake(runs_dir: Path, reps: int, expected: int | None, floor_dir: Path | None = None):
        calls.append(runs_dir)
        stats = {"median": 0.0, "max": 0.0, "min": 0.0, "n": reps}
        return {**stats, "over_empty_dir": dict(stats)} if floor_dir is not None else stats

    monkeypatch.setattr(mri, "time_cli_list", fake)
    return calls


def small_run(work: Path, *extra: str) -> list[str]:
    return [
        "--work",
        str(work),
        "--no-dashboard",
        "--list-reps",
        "1",
        "--write-reps",
        "1",
        "--samples",
        "2",
        *extra,
    ]


def listing_args() -> argparse.Namespace:
    return argparse.Namespace(
        list_reps=1, no_dashboard=True, next_dev=False, next_dev_max=0, next_dev_reps=1
    )


def probe_dir(mri: ModuleType, runs_dir: Path, count: int) -> list[str]:
    run_ids = mri.build_runs_dir(
        runs_dir, count, mri.retained_templates(), seed=count, only=mri.INDEX_INPUTS
    )
    ArtifactStore(runs_dir).rebuild_index()
    return run_ids


# --- A-1: the work directory ---


def test_folders_already_in_the_work_dir_survive(
    mri: ModuleType, cli_calls: list[Path], tmp_path: Path
) -> None:
    work = tmp_path / "work"
    names = ["retained", "probe_20", "end_to_end", "empty", "node_harness", "dashboard"]
    for name in names:
        (work / name).mkdir(parents=True)
        (work / name / "notes.txt").write_text(f"{name} belongs to someone else\n")

    code = mri.main(small_run(work, "--scales", "retained", "--probe", "20", "--end-to-end", "1"))

    assert code == 0
    assert sorted(p.name for p in work.iterdir()) == sorted(names)
    for name in names:
        assert [p.name for p in (work / name).iterdir()] == ["notes.txt"]
        assert (work / name / "notes.txt").read_text() == f"{name} belongs to someone else\n"
    # Every timed directory sat inside the directory the run created and removed.
    assert cli_calls and all(work in p.parents and p.parent.parent == work for p in cli_calls)


def test_keep_leaves_one_new_directory_and_touches_nothing_else(
    mri: ModuleType, cli_calls: list[Path], tmp_path: Path
) -> None:
    work = tmp_path / "work"
    (work / "retained").mkdir(parents=True)

    assert mri.main(small_run(work, "--scales", "retained", "--keep")) == 0

    created = [p for p in work.iterdir() if p.name != "retained"]
    assert len(created) == 1 and created[0].name.startswith("measure_run_index_")
    assert (created[0] / "retained" / "index.json").is_file()
    assert list((work / "retained").iterdir()) == []


def test_build_runs_dir_refuses_an_existing_directory(mri: ModuleType, tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    (runs_dir / "keep.txt").write_text("x")
    with pytest.raises(FileExistsError):
        mri.build_runs_dir(runs_dir, 2, mri.retained_templates(), seed=2, only=None)
    assert (runs_dir / "keep.txt").read_text() == "x"


# --- A-2: replay equals rebuild, and the index matches the dirs before timing ---


def test_a_replay_that_differs_from_rebuild_stops_the_run(
    mri: ModuleType, cli_calls: list[Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def upsert_only(store: ArtifactStore, run: Any) -> None:
        store.upsert_index_entry(RunIndexEntry.from_result(run.result, run.config))

    monkeypatch.setattr(mri, "index_ops_for_run", upsert_only)
    with pytest.raises(RuntimeError, match="replayed index differs from rebuild_index"):
        mri.main(small_run(tmp_path / "work", "--scales", "retained"))
    assert [p.name for p in cli_calls] == ["empty"]  # only the floor, before the scale
    assert list((tmp_path / "work").iterdir()) == []


def test_an_end_to_end_index_that_differs_from_rebuild_stops_the_run(
    mri: ModuleType, cli_calls: list[Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The runner never tags its runs with the batch, so the rebuild disagrees.
    monkeypatch.setattr(ArtifactStore, "enrich_index_entry_with_batch", lambda *a: None)
    with pytest.raises(RuntimeError, match="runner's index differs from rebuild_index"):
        mri.main(small_run(tmp_path / "work", "--scales", "", "--end-to-end", "1"))
    assert [p.name for p in cli_calls] == ["empty"]


@pytest.mark.parametrize(
    "damage", ["index misses a run", "stray run dir", "duplicate entry", "outdated schema"]
)
def test_listing_refuses_an_index_that_does_not_match_the_dirs(
    mri: ModuleType, cli_calls: list[Path], tmp_path: Path, damage: str
) -> None:
    runs_dir = tmp_path / "probe"
    run_ids = probe_dir(mri, runs_dir, 4)
    store = ArtifactStore(runs_dir)
    entries = store.read_index().entries
    if damage == "index misses a run":
        store._write_index(RunIndex(entries=entries[1:]))
    elif damage == "stray run dir":
        mri.copy_run(runs_dir / run_ids[0], runs_dir / "run_stray", "run_stray", None)
    elif damage == "duplicate entry":
        store._write_index(RunIndex(entries=[*entries, entries[0]]))
    else:  # read_index would rebuild this on the first listing call
        store._write_index(RunIndex(schema_version="0.4.0", entries=entries))

    with pytest.raises(RuntimeError, match="listing would time a rebuild"):
        mri.measure_listing(runs_dir, run_ids, tmp_path, listing_args())
    assert cli_calls == []


def test_listing_accepts_a_matching_index(
    mri: ModuleType, cli_calls: list[Path], tmp_path: Path
) -> None:
    runs_dir = tmp_path / "probe"
    run_ids = probe_dir(mri, runs_dir, 4)
    out = mri.measure_listing(runs_dir, run_ids, tmp_path, listing_args())
    assert cli_calls == [runs_dir]
    assert out["reader_list_runs_s"]["n"] == 1


def test_listing_refuses_a_dashboard_count_that_disagrees(
    mri: ModuleType, cli_calls: list[Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs_dir = tmp_path / "probe"
    run_ids = probe_dir(mri, runs_dir, 4)
    monkeypatch.setattr(
        mri, "time_dashboard_list", lambda *a: {"median": 0.0, "max": 0.0, "count": 3}
    )
    args = listing_args()
    args.no_dashboard = False
    with pytest.raises(RuntimeError, match="dashboard listRuns"):
        mri.measure_listing(runs_dir, run_ids, tmp_path, args)


# --- A-3: the sampled estimate leaves the index it produced ---


def test_sampling_does_not_paper_over_its_own_result(mri: ModuleType, tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    run_ids = mri.build_runs_dir(runs_dir, 6, mri.retained_templates(), seed=6, only=None)
    store = ArtifactStore(runs_dir)
    batches = mri.plan_batches(store, run_ids, 4)
    mri.replay_writes(store, batches)
    final = store.read_index().entries
    last = final[-1]
    tampered = [*final[:-1], last.model_copy(update={"batch_id": "batch_tampered"})]

    mri.sampled_write_estimate(store, batches, tampered, samples=2, reps=1)

    # The last sample rebuilt the final run's entry through the real calls.
    assert store.read_index().entries == final
    assert store.read_index().entries != tampered


def test_a_sampling_pass_that_changes_the_index_stops_the_run(
    mri: ModuleType, cli_calls: list[Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = mri.sampled_write_estimate

    def drops_the_last_entry(store: ArtifactStore, *a: Any, **kw: Any) -> dict[str, Any]:
        out = original(store, *a, **kw)
        store._write_index(RunIndex(entries=store.read_index().entries[:-1]))
        return out

    monkeypatch.setattr(mri, "sampled_write_estimate", drops_the_last_entry)
    with pytest.raises(RuntimeError, match="sampling left the index different"):
        mri.main(small_run(tmp_path / "work", "--scales", "retained"))
    assert [p.name for p in cli_calls] == ["empty"]


def test_samples_below_two_are_refused(
    mri: ModuleType, cli_calls: list[Path], tmp_path: Path
) -> None:
    with pytest.raises(SystemExit) as exc:
        mri.main(["--work", str(tmp_path / "work"), "--scales", "", "--samples", "1"])
    assert exc.value.code == 2
    assert not (tmp_path / "work").exists()


# --- A-8: next dev runs from a copy ---


def test_next_dev_runs_from_a_copy_under_the_work_dir(
    mri: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dashboard = tmp_path / "dashboard_src"
    (dashboard / "src" / "app").mkdir(parents=True)
    (dashboard / "src" / "app" / "page.tsx").write_text("export default 1;\n")
    (dashboard / "package.json").write_text("{}\n")
    (dashboard / ".next").mkdir()
    (dashboard / ".next" / "BUILD_ID").write_text("prod\n")
    (dashboard / ".env.local").write_text("SECRET=1\n")
    (dashboard / "node_modules" / ".bin").mkdir(parents=True)
    (dashboard / "node_modules" / ".bin" / "next").write_text("#!/bin/sh\n")
    monkeypatch.setattr(mri, "DASHBOARD", dashboard)

    seen: dict[str, Any] = {}

    class Stop(Exception):
        pass

    def fake_popen(command: list[str], **kwargs: Any) -> None:
        seen["command"] = command
        seen["cwd"] = Path(kwargs["cwd"])
        raise Stop

    monkeypatch.setattr(mri.subprocess, "Popen", fake_popen)
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(Stop):
        mri.time_next_dev_render(tmp_path / "runs", work, 1)

    copy = work / "dashboard"
    assert seen["cwd"] == copy
    assert Path(seen["command"][0]) == copy / "node_modules" / ".bin" / "next"
    assert (copy / "src" / "app" / "page.tsx").is_file()
    assert (copy / "node_modules").is_symlink()
    assert (copy / "node_modules").resolve() == (dashboard / "node_modules").resolve()
    assert not (copy / ".next").exists() and not (copy / ".env.local").exists()
    assert (dashboard / ".next" / "BUILD_ID").read_text() == "prod\n"
