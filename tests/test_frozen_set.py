"""The frozen evaluator (#195): what a plan freezes and when recording refuses.

Most tests run on a copy of the repository's frozen paths under tmp_path, so
they edit the real ``refund_policy.py`` and ``support_env.py`` without touching
the checkout.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from conftest import REPO_ROOT
from trace_harness.cli import main
from trace_harness.runner.collector import collect_regressions
from trace_harness.runner.experiment import Decision, ExperimentResult, ExperimentSpec
from trace_harness.runner.frozen_set import (
    CODE_COMPONENTS,
    FrozenFileChange,
    component_digest,
    compute_frozen_set,
    hash_component,
)
from trace_harness.tracing.artifact_store import ArtifactStore

ACCEPTANCE = REPO_ROOT / "docs" / "acceptance"
BASELINE = ACCEPTANCE / "experiments" / "exp_000_baseline"
BATCH = "batch_20260917T191429Z_87bfa2c7"
VERIFIER = "src/trace_harness/verifiers/refund_policy.py"
ENVIRONMENT = "src/trace_harness/environment/support_env.py"
ONE_LINE_EDITS = {
    VERIFIER: ("cash_refund_window_days: int = 30", "cash_refund_window_days: int = 31"),
    ENVIRONMENT: ("        return True, None", "        return True, 'edited'"),
}


def _checkout(dest: Path) -> Path:
    for path in [*CODE_COMPONENTS.values(), "fixtures"]:
        shutil.copytree(REPO_ROOT / path, dest / path, ignore=shutil.ignore_patterns("__pycache__"))
    return dest


def _edit(root: Path, path: str) -> None:
    old, new = ONE_LINE_EDITS[path]
    text = (root / path).read_text(encoding="utf-8")
    assert text.count(old) == 1
    (root / path).write_text(text.replace(old, new), encoding="utf-8")


def _write_plan(root: Path, **manifest) -> None:
    """exp_000's plan at the current schema, which is what a new plan looks like."""
    plan = json.loads((BASELINE / "experiment.json").read_text(encoding="utf-8"))
    del plan["schema_version"]
    plan["frozen_manifest"].update(manifest)
    (root / "experiment.json").write_text(json.dumps(plan), encoding="utf-8")


def _record(*extra: str) -> int:
    argv = ["--runs-dir", "runs", "experiment", "record", "experiment.json"]
    return main([*argv, "--condition", f"replay_only={BATCH}", *extra])


def _result(root: Path) -> ExperimentResult:
    path = root / "runs/experiments/exp_000_baseline/result.json"
    return ExperimentResult.model_validate_json(path.read_text(encoding="utf-8"))


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    """A checkout copy as the working directory, with a frozen plan and its batch."""
    root = _checkout(tmp_path / "repo")
    monkeypatch.chdir(root)
    shutil.copytree(ACCEPTANCE / "batches" / BATCH, root / "runs/batches" / BATCH)
    _write_plan(root)
    assert main(["experiment", "freeze", "experiment.json"]) == 0
    return root


# --- hashing ---


def test_component_digest_does_not_depend_on_file_order() -> None:
    files = {"b/two.py": "2" * 64, "a/one.py": "1" * 64}
    assert component_digest(files) == component_digest(dict(sorted(files.items())))


def test_component_digest_is_pinned(tmp_path) -> None:
    """Every frozen plan depends on this algorithm; changing it drifts them all."""
    (tmp_path / "t/sub").mkdir(parents=True)
    (tmp_path / "t/a.txt").write_bytes(b"one\n")
    (tmp_path / "t/sub/b.txt").write_bytes(b"two\n")
    assert hash_component(tmp_path, "t").digest == (
        "sha256:9fc681429e6ef3f39d55117e530475fdadb15ab6d1039f7c2c65e601249f3d1a"
    )


def test_a_second_checkout_hashes_the_same(tmp_path) -> None:
    """CRLF endings, reversed write order and bytecode noise change nothing."""
    first = _checkout(tmp_path / "first")
    second = tmp_path / "second"
    for source in sorted((first / "fixtures").rglob("*"), reverse=True):
        if source.is_file():
            target = second / source.relative_to(first)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes().replace(b"\n", b"\r\n"))
    (second / "fixtures/tasks/__pycache__").mkdir()
    (second / "fixtures/tasks/__pycache__/loader.cpython-312.pyc").write_bytes(b"\0")
    (second / "fixtures/.ruff_cache").mkdir()
    (second / "fixtures/.ruff_cache/CACHEDIR.TAG").write_bytes(b"\0")
    (second / "fixtures/stray.pyc").write_bytes(b"\0")
    (second / "fixtures/.DS_Store").write_bytes(b"\0")

    a = hash_component(first, "fixtures")
    b = hash_component(second, "fixtures")
    assert a.model_dump_json() == b.model_dump_json()


def test_directory_listing_order_does_not_change_the_frozen_set(monkeypatch) -> None:
    real_scandir = os.scandir

    def listed(order: bool):
        def scandir(path="."):
            with real_scandir(path) as entries:
                return _Listing(sorted(entries, key=lambda e: e.name, reverse=order))

        return scandir

    snapshots = []
    for order in (False, True):
        monkeypatch.setattr(os, "scandir", listed(order))
        frozen = compute_frozen_set(REPO_ROOT, suite_id="refund_bundles_v0")
        snapshots.append(json.dumps({n: c.model_dump() for n, c in frozen.items()}))
    assert snapshots[0] == snapshots[1]


class _Listing:
    def __init__(self, entries):
        self._entries = iter(entries)

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._entries)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def close(self) -> None:
        return None


def test_the_control_library_is_outside_the_freeze(repo) -> None:
    """Controls are what an experiment varies; a re-verified evidence index is noise."""
    frozen = ExperimentSpec.model_validate_json((repo / "experiment.json").read_text())
    assert not any(
        p.startswith("fixtures/controls/")
        for p in frozen.frozen_manifest.frozen_set["fixtures"].files
    )
    (repo / "fixtures/controls/evidence/x/y").mkdir(parents=True)
    (repo / "fixtures/controls/evidence/x/y/index.json").write_text("{}")
    assert _record() == 0
    assert _result(repo).frozen_set_verified


# --- recording ---


def test_an_unchanged_tree_records_as_verified(repo) -> None:
    assert _record("--decision", "keep", "--allow-drift") == 0
    result = _result(repo)
    assert result.frozen_set_verified and not result.frozen_set_drifted
    assert result.decision is Decision.KEEP  # --allow-drift does nothing without drift


@pytest.mark.parametrize("path", [VERIFIER, ENVIRONMENT])
def test_a_one_line_edit_blocks_recording_with_the_file_named(repo, capsys, path) -> None:
    _edit(repo, path)
    capsys.readouterr()
    assert _record() == 2
    err = capsys.readouterr().err
    assert f"changed {path}" in err
    assert "recording is refused" in err
    assert not (repo / "runs/experiments/exp_000_baseline/result.json").exists()


def test_added_and_removed_files_are_named(repo, capsys) -> None:
    (repo / "src/trace_harness/verifiers/extra_check.py").write_text("X = 1\n")
    (repo / "src/trace_harness/attribution/validation.py").unlink()
    capsys.readouterr()
    assert _record() == 2
    err = capsys.readouterr().err
    assert "verifiers: added src/trace_harness/verifiers/extra_check.py" in err
    assert "attribution: removed src/trace_harness/attribution/validation.py" in err


def test_a_named_labels_file_is_frozen(tmp_path, monkeypatch, capsys) -> None:
    root = _checkout(tmp_path / "repo")
    monkeypatch.chdir(root)
    shutil.copytree(ACCEPTANCE / "batches" / BATCH, root / "runs/batches" / BATCH)
    (root / "labels.jsonl").write_text('{"run_id": "r1", "root_cause_step": 3}\n')
    _write_plan(root, labels_path="labels.jsonl")
    assert main(["experiment", "freeze", "experiment.json"]) == 0
    (root / "labels.jsonl").write_text('{"run_id": "r1", "root_cause_step": 4}\n')
    capsys.readouterr()
    assert _record() == 2
    assert "labels: changed labels.jsonl" in capsys.readouterr().err


def test_allow_drift_marks_the_result_and_forces_review(repo, capsys) -> None:
    _edit(repo, VERIFIER)
    assert _record("--decision", "keep", "--allow-drift") == 0
    result = _result(repo)
    assert result.frozen_set_drifted and not result.frozen_set_verified
    assert result.decision is Decision.REVIEW
    assert result.frozen_set_drift == [
        FrozenFileChange(component="verifiers", path=VERIFIER, change="changed")
    ]
    assert "--decision keep overridden" in capsys.readouterr().out
    report = (repo / "runs/experiments/exp_000_baseline/report.md").read_text()
    assert "Frozen set drifted" in report and VERIFIER in report


@pytest.mark.parametrize("decision", [d for d in Decision if d is not Decision.REVIEW])
def test_a_drifted_result_cannot_carry_another_decision(decision) -> None:
    change = FrozenFileChange(component="verifiers", path=VERIFIER, change="changed")
    with pytest.raises(ValidationError, match="must have decision review"):
        ExperimentResult(
            experiment_id="exp",
            decision=decision,
            decided_by="human",
            frozen_set_drifted=True,
            frozen_set_drift=[change],
        )


def test_a_plan_is_frozen_once(repo, capsys) -> None:
    capsys.readouterr()
    assert main(["experiment", "freeze", "experiment.json"]) == 2
    assert "already frozen" in capsys.readouterr().err


def test_a_current_plan_without_a_frozen_set_is_refused(repo, capsys) -> None:
    _write_plan(repo)
    capsys.readouterr()
    assert _record() == 2
    assert "experiment freeze" in capsys.readouterr().err


def test_fixtures_hash_has_to_match_the_frozen_digest(repo) -> None:
    plan = json.loads((repo / "experiment.json").read_text())
    plan["frozen_manifest"]["fixtures_hash"] = "sha256:01e4172931eda28c"
    with pytest.raises(ValidationError, match="disagrees with the frozen fixtures digest"):
        ExperimentSpec.model_validate(plan)


# --- plans and results from before the frozen set ---


def test_exp_000_records_and_says_nothing_was_checked(tmp_path, monkeypatch) -> None:
    """A 0.1.0 plan records unchanged, and its result never reads as verified."""
    monkeypatch.chdir(REPO_ROOT)
    runs = tmp_path / "runs"
    shutil.copytree(ACCEPTANCE / "batches" / BATCH, runs / "batches" / BATCH)
    plan = BASELINE / "experiment.json"
    before = plan.read_bytes()
    argv = ["--runs-dir", str(runs), "experiment", "record", str(plan)]
    assert main([*argv, "--condition", f"replay_only={BATCH}"]) == 0
    assert plan.read_bytes() == before
    result = ExperimentResult.model_validate_json(
        (runs / "experiments/exp_000_baseline/result.json").read_text()
    )
    assert result.decision is Decision.BASELINE
    assert not result.frozen_set_verified and not result.frozen_set_drifted


def test_the_retained_result_loads_as_unchecked() -> None:
    result = ExperimentResult.model_validate_json((BASELINE / "result.json").read_text())
    assert not result.frozen_set_verified and not result.frozen_set_drifted


# --- the collector over retained experiments ---


def _retain(repo: Path) -> Path:
    """Record in the copy and keep the experiment the way docs/acceptance does."""
    assert _record() == 0
    retained = repo / "retained"
    shutil.copytree(repo / "runs/experiments", retained)
    return retained


def _collect(repo: Path, experiments: Path):
    (repo / "no-artifacts").mkdir(exist_ok=True)
    store = ArtifactStore(repo / "gate")
    return collect_regressions(repo / "no-artifacts", store, experiments_path=experiments)


def test_a_later_verifier_edit_warns_without_failing_the_gate(repo) -> None:
    retained = _retain(repo)
    clean = _collect(repo, retained)
    assert [e.status for e in clean.experiments] == ["matches"]

    _edit(repo, VERIFIER)
    summary = _collect(repo, retained)
    assert summary.exit_code == 0
    assert summary.experiments_drifted == ["exp_000_baseline"]
    (entry,) = summary.experiments
    assert entry.status == "drifted" and not entry.recorded_drifted
    assert [c.path for c in entry.changes] == [VERIFIER]


def test_a_result_recorded_over_drift_is_reported_as_such(repo) -> None:
    _edit(repo, ENVIRONMENT)
    assert _record("--allow-drift") == 0
    retained = repo / "retained"
    shutil.copytree(repo / "runs/experiments", retained)
    summary = _collect(repo, retained)
    assert summary.exit_code == 0
    (entry,) = summary.experiments
    assert entry.status == "drifted" and entry.recorded_drifted


def test_a_retained_experiment_that_does_not_load_fails_the_gate(repo) -> None:
    retained = _retain(repo)
    result = retained / "exp_000_baseline/result.json"
    data = json.loads(result.read_text())
    data["decision"] = "keep"
    data["frozen_set_drifted"] = True
    data["frozen_set_drift"] = [{"component": "verifiers", "path": VERIFIER, "change": "changed"}]
    data["frozen_set_verified"] = False
    result.write_text(json.dumps(data))
    summary = _collect(repo, retained)
    assert summary.exit_code == 2
    assert summary.malformed == [str(retained / "exp_000_baseline")]


def test_the_repository_experiments_pass_the_gate(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(REPO_ROOT)
    (tmp_path / "empty").mkdir()
    summary = collect_regressions(
        tmp_path / "empty",
        ArtifactStore(tmp_path / "gate"),
        experiments_path=ACCEPTANCE / "experiments",
    )
    assert summary.exit_code == 0
    statuses = {e.experiment_id: e.status for e in summary.experiments}
    assert statuses["exp_000_baseline"] == "not_recorded"
