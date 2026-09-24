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
from trace_harness.runner.collector import SUMMARY_NAME, collect_regressions
from trace_harness.runner.experiment import (
    Decision,
    ExperimentResult,
    ExperimentSpec,
    FrozenManifest,
)
from trace_harness.runner.frozen_set import (
    CODE_COMPONENTS,
    FrozenComponent,
    FrozenFileChange,
    FrozenSetError,
    component_digest,
    compute_frozen_set,
    freeze,
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


def test_the_control_library_is_frozen(repo, capsys) -> None:
    """Brief 001 keeps existing control entries as registered once the runs start."""
    frozen = ExperimentSpec.model_validate_json((repo / "experiment.json").read_text())
    files = frozen.frozen_manifest.frozen_set["fixtures"].files
    assert "fixtures/controls/library.json" in files
    assert any(p.startswith("fixtures/controls/evidence/") for p in files)
    library = repo / "fixtures/controls/library.json"
    library.write_text(library.read_text().replace('"active"', '"retired"', 1))
    capsys.readouterr()
    assert _record() == 2
    assert "fixtures: changed fixtures/controls/library.json" in capsys.readouterr().err


def test_a_generated_evidence_index_is_the_only_exclusion(repo, capsys) -> None:
    """Re-verifying retained evidence writes index.json beside its runs, and nothing else."""
    evidence = repo / "fixtures/controls/evidence"
    (evidence / "x/y/z").mkdir(parents=True)
    (evidence / "x/y/index.json").write_text("{}")
    assert _record() == 0
    assert _result(repo).frozen_set_verified

    (evidence / "x/index.json").write_text("{}")
    (evidence / "x/y/z/index.json").write_text("{}")
    capsys.readouterr()
    assert _record() == 2
    err = capsys.readouterr().err
    assert "fixtures: added fixtures/controls/evidence/x/index.json" in err
    assert "fixtures: added fixtures/controls/evidence/x/y/z/index.json" in err
    assert "evidence/x/y/index.json" not in err


# --- what the frozen set refuses to hash ---


@pytest.mark.parametrize("kind", ["directory", "file"])
def test_a_symlink_inside_a_frozen_component_is_refused(repo, tmp_path, capsys, kind) -> None:
    """os.walk skips a linked directory, so its files would leave the hash unnoticed."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "rule.py").write_text("X = 1\n")
    target = outside if kind == "directory" else outside / "rule.py"
    (repo / "src/trace_harness/verifiers/linked").symlink_to(target)
    capsys.readouterr()
    assert _record() == 2
    err = capsys.readouterr().err
    assert "src/trace_harness/verifiers/linked is a symlink" in err
    assert not (repo / "runs/experiments/exp_000_baseline/result.json").exists()

    _write_plan(repo)
    assert main(["experiment", "freeze", "experiment.json"]) == 2
    assert "src/trace_harness/verifiers/linked is a symlink" in capsys.readouterr().err


@pytest.mark.parametrize(
    "labels_path",
    ["", ".", "..", "../labels.jsonl", "a/../labels.jsonl", "./labels.jsonl", "a//labels.jsonl"]
    + ["/tmp/labels.jsonl", "C:/labels.jsonl", "a\\labels.jsonl"],
)
def test_labels_path_has_to_name_a_path_inside_the_repository(labels_path) -> None:
    """'', '.' and '..' would freeze the whole tree or its parent as the labels."""
    manifest = {"suite_id": "refund_bundles_v0", "fixtures_hash": "sha256:x"}
    with pytest.raises(ValidationError, match="labels_path"):
        FrozenManifest.model_validate({**manifest, "labels_path": labels_path})


def test_freeze_refuses_labels_that_are_a_directory(repo, capsys) -> None:
    (repo / "labels").mkdir()
    (repo / "labels/a.jsonl").write_text("{}\n")
    _write_plan(repo, labels_path="labels")
    capsys.readouterr()
    assert main(["experiment", "freeze", "experiment.json"]) == 2
    assert "labels_path must name a file" in capsys.readouterr().err


def test_freeze_refuses_labels_that_resolve_outside_the_repository(repo, tmp_path, capsys):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "labels.jsonl").write_text("{}\n")
    (repo / "data").symlink_to(outside)
    _write_plan(repo, labels_path="data/labels.jsonl")
    capsys.readouterr()
    assert main(["experiment", "freeze", "experiment.json"]) == 2
    assert "resolves outside" in capsys.readouterr().err


def test_freeze_refuses_labels_that_are_a_symlink(repo, capsys) -> None:
    (repo / "labels.jsonl").write_text("{}\n")
    (repo / "linked.jsonl").symlink_to(repo / "labels.jsonl")
    _write_plan(repo, labels_path="linked.jsonl")
    capsys.readouterr()
    assert main(["experiment", "freeze", "experiment.json"]) == 2
    assert "linked.jsonl is a symlink" in capsys.readouterr().err


def test_freeze_called_directly_refuses_an_absolute_labels_path(repo) -> None:
    (repo / "labels.jsonl").write_text("{}\n")
    with pytest.raises(FrozenSetError, match="labels_path must be a relative POSIX path"):
        freeze(repo, suite_id="refund_bundles_v0", labels_path=str(repo / "labels.jsonl"))


def test_freeze_refuses_a_missing_path(repo, capsys) -> None:
    _write_plan(repo, suite_id="no_such_suite")
    capsys.readouterr()
    assert main(["experiment", "freeze", "experiment.json"]) == 2
    assert "cannot freeze fixtures/suites/no_such_suite.json: not found" in capsys.readouterr().err


def test_freeze_refuses_a_suite_file_that_names_another_suite(repo, capsys) -> None:
    source = repo / "fixtures/suites/refund_bundles_v0.json"
    shutil.copy(source, repo / "fixtures/suites/renamed.json")
    _write_plan(repo, suite_id="renamed")
    capsys.readouterr()
    assert main(["experiment", "freeze", "experiment.json"]) == 2
    assert "declares suite_id 'refund_bundles_v0' where the plan names 'renamed'" in (
        capsys.readouterr().err
    )


def test_freeze_refuses_a_missing_plan(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["experiment", "freeze", "missing.json"]) == 2
    assert "experiment plan not found: missing.json" in capsys.readouterr().err


def test_record_outside_the_repository_root_is_refused(repo, tmp_path, monkeypatch, capsys):
    """Every frozen file would otherwise be listed as removed."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    capsys.readouterr()
    argv = ["--runs-dir", str(repo / "runs"), "experiment", "record", str(repo / "experiment.json")]
    assert main([*argv, "--condition", f"replay_only={BATCH}"]) == 2
    err = capsys.readouterr().err
    assert "run from the repository root" in err
    assert "removed" not in err


def test_a_component_digest_has_to_match_its_files() -> None:
    component = hash_component(REPO_ROOT, VERIFIER).model_dump()
    FrozenComponent.model_validate(component)
    with pytest.raises(ValidationError, match="does not match its files"):
        FrozenComponent.model_validate({**component, "digest": f"sha256:{'0' * 64}"})
    with pytest.raises(ValidationError, match="does not match its files"):
        FrozenComponent.model_validate({**component, "files": {VERIFIER: "0" * 64}})


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


@pytest.mark.parametrize(
    ("flags", "message"),
    [
        ({"frozen_set_verified": True, "frozen_set_drifted": True}, "both verified and drifted"),
        ({"frozen_set_drifted": True}, "exactly when frozen_set_drift is set"),
        ({"frozen_set_drift": [{"component": "suite", "path": "s", "change": "added"}]}, "exactly"),
    ],
    ids=["verified-and-drifted", "drifted-without-files", "files-without-drifted"],
)
def test_a_result_has_to_say_one_thing_about_its_frozen_set(flags, message) -> None:
    with pytest.raises(ValidationError, match=message):
        ExperimentResult(experiment_id="exp", decision="review", decided_by="human", **flags)


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


def _edit_retained(retained: Path, name: str, **fields) -> None:
    path = retained / "exp_000_baseline" / name
    data = json.loads(path.read_text())
    if "frozen_manifest" in fields:
        data["frozen_manifest"].update(fields.pop("frozen_manifest"))
    data.update(fields)
    path.write_text(json.dumps(data))


UNFROZEN = {"frozen_manifest": {"frozen_set": None}}
DRIFTED = {
    "decision": "review",
    "frozen_set_verified": False,
    "frozen_set_drifted": True,
    "frozen_set_drift": [{"component": "verifiers", "path": VERIFIER, "change": "changed"}],
}


@pytest.mark.parametrize(
    ("plan", "result", "message"),
    [
        (
            {"schema_version": "0.1.0", **UNFROZEN},
            {},
            "the result claims a frozen-set check, but the plan has no frozen set",
        ),
        (
            {"schema_version": "0.1.0", **UNFROZEN},
            DRIFTED,
            "the result claims a frozen-set check, but the plan has no frozen set",
        ),
        (
            {},
            {"frozen_set_verified": False},
            "the plan is frozen, but the result records no frozen-set check",
        ),
        (
            UNFROZEN,
            {"frozen_set_verified": False},
            "plan schema 0.2.0 has no frozen set",
        ),
    ],
    ids=[
        "verified-result-unfrozen-plan",
        "drifted-result-unfrozen-plan",
        "frozen-plan-unchecked-result",
        "current-plan-without-frozen-set",
    ],
)
def test_a_plan_and_result_record_could_not_have_written_fail_the_gate(
    repo, plan, result, message
) -> None:
    """Each pair loads on its own; together they contradict what record writes."""
    retained = _retain(repo)
    _edit_retained(retained, "experiment.json", **json.loads(json.dumps(plan)))
    _edit_retained(retained, "result.json", **json.loads(json.dumps(result)))
    summary = _collect(repo, retained)
    assert summary.exit_code == 2
    assert summary.malformed == [str(retained / "exp_000_baseline")]
    assert summary.experiments == []
    assert any(message in error for error in summary.errors), summary.errors


def test_a_plan_awaiting_freeze_without_a_result_passes(repo) -> None:
    """A registered plan is committed before it is frozen, as brief 001's runbook does."""
    retained = repo / "retained"
    (retained / "exp_000_baseline").mkdir(parents=True)
    _write_plan(retained / "exp_000_baseline")
    summary = _collect(repo, retained)
    assert summary.exit_code == 0
    assert [e.status for e in summary.experiments] == ["not_recorded"]


def test_an_absolute_labels_path_is_malformed_and_the_summary_is_written(repo, tmp_path):
    retained = _retain(repo)
    _edit_retained(
        retained, "experiment.json", frozen_manifest={"labels_path": str(repo / "fixtures")}
    )
    summary = _collect(repo, retained)
    assert summary.exit_code == 2
    assert summary.malformed == [str(retained / "exp_000_baseline")]
    assert (repo / "gate" / SUMMARY_NAME).is_file()


def test_a_tree_that_cannot_be_hashed_is_malformed_and_the_summary_is_written(repo, tmp_path):
    retained = _retain(repo)
    (tmp_path / "outside").mkdir()
    (repo / "src/trace_harness/environment/linked").symlink_to(tmp_path / "outside")
    summary = _collect(repo, retained)
    assert summary.exit_code == 2
    assert summary.malformed == [str(retained / "exp_000_baseline")]
    assert any("is a symlink" in error for error in summary.errors)
    assert (repo / "gate" / SUMMARY_NAME).is_file()


def test_the_collector_outside_the_repository_root_is_malformed(repo, tmp_path, monkeypatch):
    retained = _retain(repo)
    (repo / "no-artifacts").mkdir()
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.chdir(tmp_path / "elsewhere")
    summary = collect_regressions(
        repo / "no-artifacts", ArtifactStore(repo / "gate"), experiments_path=retained
    )
    assert summary.exit_code == 2
    assert any("run from the repository root" in error for error in summary.errors)


def test_a_missing_experiments_directory_fails_the_gate(repo) -> None:
    summary = _collect(repo, repo / "no-such-dir")
    assert summary.exit_code == 2
    assert summary.errors == [f"experiments directory not found: {repo / 'no-such-dir'}"]
