"""Staging the retained tree and turning RunReader's answers into table rows."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

import postgrest_fake as fake
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


def test_staging_walks_into_an_experiment_for_the_runs_retained_there(tmp_path: Path) -> None:
    """A branch experiment retains its fork points inside its own folder (#200)."""
    root = tmp_path / "retained"
    experiment = ACCEPTANCE / "experiments" / "exp_000_baseline"
    copied = root / "experiments" / experiment.name
    shutil.copytree(experiment, copied)
    fork_point = retained_run_dirs()[0]
    shutil.copytree(fork_point, copied / "fork_points" / fork_point.name)
    (copied / "fork_points" / names.RUN_INDEX).write_text('{"entries": []}', encoding="utf-8")
    (copied / names.RUN_INDEX).write_text('{"entries": []}', encoding="utf-8")

    staged = stage_retained(root, tmp_path / "staged")

    assert staged.runs == {
        fork_point.name: f"experiments/{experiment.name}/fork_points/{fork_point.name}"
    }
    assert staged.experiments == {experiment.name: f"experiments/{experiment.name}"}
    own = sorted(p.name for p in experiment.iterdir() if p.is_file())
    staged_experiment = staged.runs_dir / names.EXPERIMENTS_DIR / experiment.name
    assert sorted(p.name for p in staged_experiment.iterdir()) == own
    assert not list(staged.runs_dir.rglob(names.RUN_INDEX)), "an index file was staged"
    built = build_rows(RunReader.from_runs_dir(staged.runs_dir), sorted(staged.batches))
    assert [r["run_id"] for r in built[schema.RUNS]] == [fork_point.name]
    assert [r["experiment_id"] for r in built[schema.EXPERIMENTS]] == [experiment.name]


# --- reproductions of an earlier card (#211) -------------------------------------


def test_a_reproduction_row_names_the_card_run_and_holds_no_copy(tmp_path: Path) -> None:
    root = fake.retained_with_reproduction(tmp_path / "retained")
    reader, staged, built = fake.reproduction_rows(root, tmp_path / "staged")

    assert staged.bundle_refs == {fake.REPRODUCTION_RUN: fake.CARD_RUN}
    # The reader follows the pointer, as RunReader does since #211.
    assert reader.get_bundle(fake.REPRODUCTION_RUN) == reader.get_bundle(fake.CARD_RUN)
    by_id = {row["run_id"]: row for row in built[schema.RUNS]}
    reproduction, card = by_id[fake.REPRODUCTION_RUN], by_id[fake.CARD_RUN]
    assert reproduction["canonical_run_id"] == fake.CARD_RUN
    assert all(reproduction[column] is None for column in fake.BUNDLE_COLUMNS)
    assert card["canonical_run_id"] is None
    assert card["failure_card"] == reader.get_bundle(fake.CARD_RUN).failure_card.model_dump(
        mode="json"
    )
    # Everything else about the reproduction is its own.
    assert reproduction["verifier_result"]["run_id"] == fake.REPRODUCTION_RUN


def test_a_card_of_its_own_wins_over_a_pointer_beside_it(tmp_path: Path) -> None:
    root = tmp_path / "retained"
    shutil.copytree(fake.retained_run_dir(fake.CARD_RUN), root / fake.CARD_RUN)
    ref = fake.bundle_ref(fake.CARD_RUN, fake.REPRODUCTION_RUN)
    (root / fake.CARD_RUN / "bundle_ref.json").write_text(json.dumps(ref), encoding="utf-8")
    staged = stage_retained(root, tmp_path / "staged")
    assert staged.bundle_refs == {}


def test_staging_refuses_a_pointer_to_a_run_that_is_not_retained(tmp_path: Path) -> None:
    root = fake.retained_with_reproduction(tmp_path / "retained", with_card_run=False)
    with pytest.raises(ValueError) as refused:
        stage_retained(root, tmp_path / "staged")
    message = str(refused.value)
    assert f"run '{fake.REPRODUCTION_RUN}' (live/{fake.REPRODUCTION_RUN})" in message
    assert f"'{fake.CARD_RUN}' is not retained" in message


def test_staging_refuses_a_pointer_to_a_run_without_a_card(tmp_path: Path) -> None:
    root = fake.retained_with_reproduction(tmp_path / "retained", canonical_run_id=fake.PASSING_RUN)
    shutil.copytree(fake.retained_run_dir(fake.PASSING_RUN), root / "runs" / fake.PASSING_RUN)
    with pytest.raises(ValueError, match=f"'{fake.PASSING_RUN}' holds no failure_card.json"):
        stage_retained(root, tmp_path / "staged")


@pytest.mark.parametrize(
    "canonical", [None, "", "..", "../elsewhere", "a/b", fake.REPRODUCTION_RUN, 7]
)
def test_staging_refuses_a_pointer_that_names_no_usable_run(
    canonical: object, tmp_path: Path
) -> None:
    root = fake.retained_with_reproduction(tmp_path / "retained", canonical_run_id=canonical)
    with pytest.raises(ValueError, match="names no usable canonical_run_id"):
        stage_retained(root, tmp_path / "staged")


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
