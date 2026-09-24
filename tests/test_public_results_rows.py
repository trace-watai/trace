"""Staging the retained tree and turning RunReader's answers into table rows."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from trace_harness.public_results import schema
from trace_harness.public_results.retained import stage_retained
from trace_harness.public_results.rows import build_rows, content_sha256
from trace_harness.run_reader import RunReader
from trace_harness.tracing import artifact_store as names

REPO_ROOT = Path(__file__).resolve().parents[1]
ACCEPTANCE = REPO_ROOT / "docs" / "acceptance"


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def retained_run_dirs() -> list[Path]:
    return sorted(p.parent for p in ACCEPTANCE.rglob(names.RUN_RESULT))


def test_staging_copies_every_retained_item_and_no_index(tmp_path: Path) -> None:
    staged = stage_retained(ACCEPTANCE, tmp_path / "staged")

    assert sorted(staged.runs) == sorted(p.name for p in retained_run_dirs())
    for run_id, source in staged.runs.items():
        original = ACCEPTANCE / source
        copied = staged.runs_dir / run_id
        assert sorted(p.name for p in copied.iterdir()) == sorted(
            p.name for p in original.iterdir()
        )
    loose = [p for p in ACCEPTANCE.rglob("*" + names.BATCH_SUMMARY)]
    assert len(staged.batches) == len(loose) >= 2
    for batch_id in staged.batches:
        assert (staged.runs_dir / names.BATCHES_DIR / batch_id / names.BATCH_SUMMARY).is_file()
    assert sorted(staged.experiments) == sorted(
        p.parent.name for p in ACCEPTANCE.rglob(names.EXPERIMENT_SPEC)
    )
    assert not list(staged.runs_dir.rglob(names.RUN_INDEX)), "an index file was staged"


def test_reading_the_staged_copy_leaves_the_retained_tree_untouched(tmp_path: Path) -> None:
    before = tree_digest(ACCEPTANCE)
    staged = stage_retained(ACCEPTANCE, tmp_path / "staged")
    build_rows(RunReader.from_runs_dir(staged.runs_dir), sorted(staged.batches))
    assert tree_digest(ACCEPTANCE) == before
    # RunReader rebuilt the staged copy's index from the artifacts.
    assert (staged.runs_dir / names.RUN_INDEX).is_file()


def test_staging_refuses_the_same_id_from_two_places(tmp_path: Path) -> None:
    root = tmp_path / "retained"
    first = retained_run_dirs()[0]
    shutil.copytree(first, root / "a" / first.name)
    shutil.copytree(first, root / "b" / first.name)
    with pytest.raises(ValueError, match="retained twice"):
        stage_retained(root, tmp_path / "staged")


def test_staging_refuses_a_batch_summary_without_an_id_and_a_dirty_destination(
    tmp_path: Path,
) -> None:
    root = tmp_path / "retained"
    root.mkdir()
    (root / "odd_batch_summary.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="no usable batch_id"):
        stage_retained(root, tmp_path / "staged")
    dirty = tmp_path / "dirty"
    dirty.mkdir()
    (dirty / "leftover").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="not empty"):
        stage_retained(ACCEPTANCE, dirty)
    with pytest.raises(ValueError, match="not found"):
        stage_retained(tmp_path / "missing", tmp_path / "staged2")


@pytest.fixture(scope="module")
def rows(tmp_path_factory: pytest.TempPathFactory) -> dict[str, list[dict]]:
    staged = stage_retained(ACCEPTANCE, tmp_path_factory.mktemp("rows") / "staged")
    return build_rows(RunReader.from_runs_dir(staged.runs_dir), sorted(staged.batches))


def test_rows_have_the_schema_columns_and_keep_the_json_contracts(rows) -> None:
    for table, table_rows in rows.items():
        assert table_rows
        for row in table_rows:
            assert tuple(row) == schema.COLUMNS[table]
            assert row[schema.CONTENT_SHA256] == content_sha256(table, row)
    for row in rows[schema.RUNS]:
        assert row["summary"]["run_id"] == row["run_result"]["run_id"] == row["run_id"]
        assert row["summary"]["batch_id"] == row["batch_id"]
        assert isinstance(row["trace"], list) and row["trace"]
        bundle = [row[c] for c in ("failure_card", "repair_package", "regression_artifact")]
        assert all(part is None for part in bundle) or all(part is not None for part in bundle)
        assert "schema_version" in row["run_result"]
    # Runs of the refund_v0 batch get their batch id back from the staged
    # summary, as the retained index says.
    retained_index = json.loads((ACCEPTANCE / "runs" / names.RUN_INDEX).read_text())
    by_id = {r["run_id"]: r for r in rows[schema.RUNS]}
    for entry in retained_index["entries"]:
        assert by_id[entry["run_id"]]["batch_id"] == entry["batch_id"]


def test_rows_are_the_same_on_every_build(rows, tmp_path: Path) -> None:
    staged = stage_retained(ACCEPTANCE, tmp_path / "again")
    again = build_rows(RunReader.from_runs_dir(staged.runs_dir), sorted(staged.batches))
    for table in rows:
        assert [r[schema.CONTENT_SHA256] for r in again[table]] == [
            r[schema.CONTENT_SHA256] for r in rows[table]
        ]


def test_the_hash_ignores_only_the_suite_report_stamp(rows) -> None:
    batch = rows[schema.BATCHES][0]
    restamped = batch | {"suite_report": batch["suite_report"] | {"generated_at": "2000-01-01"}}
    assert content_sha256(schema.BATCHES, restamped) == batch[schema.CONTENT_SHA256]
    edited = batch | {"suite_report": batch["suite_report"] | {"total_rows": -1}}
    assert content_sha256(schema.BATCHES, edited) != batch[schema.CONTENT_SHA256]
    run = rows[schema.RUNS][0]
    restamped_run = run | {"run_result": run["run_result"] | {"generated_at": "x"}}
    assert content_sha256(schema.RUNS, restamped_run) != run[schema.CONTENT_SHA256]
