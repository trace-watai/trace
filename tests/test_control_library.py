"""Control promotion, retained provenance, rollback, suite behavior (#147), and
the acceptance basis each entry records under ADR-0002.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import shutil

import pytest

from conftest import FIXTURES_DIR
from trace_harness import cli
from trace_harness.cli import main
from trace_harness.environment import controls as controls_module
from trace_harness.environment.control_library import (
    CONTROL_LIBRARY_SCHEMA_VERSION,
    AcceptanceBasis,
    ControlLibrary,
    check_acceptance,
    load_library,
    rollback_control,
)
from trace_harness.environment.controls import (
    GUARDRAIL_REGISTRY,
    REFUND_WINDOW_CONTROL_ID,
    RegisteredGuardrail,
    RuleRef,
    reference_controls,
)
from trace_harness.environment.support_env import SupportEnvironment
from trace_harness.environment.tools import ToolResult
from trace_harness.failure_bundles.schemas import RepairPackage
from trace_harness.regression.repair_validation import RepairValidation
from trace_harness.regression.schemas import RegressionArtifact
from trace_harness.runner.batch import BatchRunner, BatchSummary
from trace_harness.runner.result import RunResult
from trace_harness.runner.suite import load_suite
from trace_harness.tasks.loader import load_task
from trace_harness.tracing import artifact_store as names
from trace_harness.tracing.artifact_store import ArtifactStore

DEMO = FIXTURES_DIR / "tasks/refund_policy_control_demo.json"
VALID = FIXTURES_DIR / "tasks/refund_policy_valid_cash.json"
SUITE = FIXTURES_DIR / "suites/refund_v0.json"
COMMITTED_LIBRARY = FIXTURES_DIR / "controls/library.json"


def _edit(path, update):
    data = json.loads(path.read_text())
    update(data)
    path.write_text(json.dumps(data, indent=2) + "\n")


def _bundle(tmp_path, *, sibling=True):
    source = tmp_path / "source"
    assert main(["--runs-dir", str(source), "run-pipeline", str(DEMO)]) == 0
    (run_dir,) = [p for p in source.iterdir() if p.is_dir()]
    artifact = run_dir / names.REGRESSION_ARTIFACT
    if sibling:
        _edit(
            artifact,
            lambda a: a.update(
                positive_sibling_tests=[
                    {
                        "test_name": "valid_cash_refund",
                        "task_fixture": str(VALID),
                    }
                ]
            ),
        )
    return artifact


def _commit(tmp_path, artifact, library, *args):
    return main(
        [
            "--runs-dir",
            str(tmp_path / "replay"),
            "replay",
            str(artifact),
            "--apply-control",
            "--commit",
            "--control-library",
            str(library),
            *args,
        ]
    )


@pytest.fixture
def committed(tmp_path):
    artifact = _bundle(tmp_path)
    library = tmp_path / "controls/library.json"
    assert _commit(tmp_path, artifact, library) == 0
    return library, artifact


def test_commit_retains_evidence_and_installs_control(committed):
    path, artifact = committed
    library = load_library(path)
    (entry,) = library.entries
    assert library.schema_version == CONTROL_LIBRARY_SCHEMA_VERSION
    assert entry.control.control_id == REFUND_WINDOW_CONTROL_ID
    # The demo artifact is live_required, so its acceptance is advisory.
    assert entry.acceptance == AcceptanceBasis(
        replay_mode="live_required", predicted_by="heuristic_v1", standing="advisory"
    )
    assert entry.control.provenance.run_id == json.loads(artifact.read_text())["source_run_id"]
    assert entry.status == "active"
    assert [h.status for h in entry.history] == ["active"]
    assert all(ref.resolve(path.parent).is_file() for ref in entry.provenance.refs())
    validation = json.loads(entry.provenance.repair_validation.read(path.parent))
    accepted = next(c for c in validation["controls"] if c["verdict"] == "accepted")
    assert accepted["sibling_reruns"][0]["verdict"] == "PASS"
    check = json.loads(entry.provenance.activation_check.read(path.parent))
    assert len(check["regressions"][0]["run_ids"]) == 2
    assert [c["control_id"] for c in check["controls"]] == [REFUND_WINDOW_CONTROL_ID]
    env = SupportEnvironment.from_task(load_task(DEMO), control_library=path)
    assert env.installed_controls == library.active_controls()


def test_plain_validation_does_not_change_library(committed, tmp_path):
    library, artifact = committed
    before = {
        p.relative_to(library.parent): p.read_bytes()
        for p in library.parent.rglob("*")
        if p.is_file()
    }
    assert (
        main(
            [
                "--runs-dir",
                str(tmp_path / "validation-only"),
                "replay",
                str(artifact),
                "--apply-control",
            ]
        )
        == 0
    )
    after = {
        p.relative_to(library.parent): p.read_bytes()
        for p in library.parent.rglob("*")
        if p.is_file()
    }
    assert after == before


def test_replay_library_uses_only_active_entries(committed, tmp_path):
    library, artifact = committed
    output = tmp_path / "library-replay"
    args = ["--runs-dir", str(output), "replay", str(artifact), "--control-library", str(library)]
    assert main(args) == 0
    assert not list(output.rglob(names.REPAIR_VALIDATION))
    rollback_control(library, REFUND_WINDOW_CONTROL_ID, "restore baseline")
    assert main(args) == 1


@pytest.mark.parametrize("command", ["run-fixture", "run-pipeline"])
def test_single_run_loads_library(committed, tmp_path, command):
    library, _ = committed
    output = tmp_path / command
    assert (
        main(
            [
                "--runs-dir",
                str(output),
                command,
                str(DEMO),
                "--control-library",
                str(library),
            ]
        )
        == 0
    )
    (run_dir,) = [p for p in output.iterdir() if p.is_dir()]
    state = json.loads((run_dir / names.FINAL_STATE).read_text())
    config = json.loads((run_dir / names.RUN_CONFIG).read_text())
    assert state["refunds"] == []
    assert [c["control_id"] for c in config["metadata"]["controls"]] == [REFUND_WINDOW_CONTROL_ID]


def test_rollback_preserves_history_and_provenance(committed):
    path, _ = committed
    before = load_library(path).entries[0]
    assert (
        main(
            [
                "controls",
                "rollback",
                REFUND_WINDOW_CONTROL_ID,
                "--control-library",
                str(path),
                "--reason",
                "review requested a rollback",
            ]
        )
        == 0
    )
    after = load_library(path)
    assert after.active_controls() == []
    entry = after.entries[0]
    assert entry.provenance == before.provenance
    assert entry.control == before.control
    assert entry.history[0] == before.history[0]
    assert [h.status for h in entry.history] == ["active", "rolled_back"]
    assert entry.history[-1].reason == "review requested a rollback"
    saved = path.read_bytes()
    with pytest.raises(ValueError, match="not active"):
        rollback_control(path, REFUND_WINDOW_CONTROL_ID, "again")
    assert path.read_bytes() == saved


@pytest.mark.parametrize("reason", ["", "  \n "])
def test_rollback_requires_a_nonempty_reason(committed, reason):
    path, _ = committed
    before = path.read_bytes()
    with pytest.raises(ValueError):
        rollback_control(path, REFUND_WINDOW_CONTROL_ID, reason)
    assert path.read_bytes() == before


def test_duplicate_promotion_preserves_manifest_and_evidence(committed, tmp_path):
    library, artifact = committed
    before = library.read_bytes()
    bundles = set((library.parent / "evidence").iterdir())
    assert _commit(tmp_path, artifact, library) == 2
    assert library.read_bytes() == before
    assert set((library.parent / "evidence").iterdir()) == bundles


@pytest.mark.parametrize(
    "field",
    [
        "source_run",
        "repair_package",
        "repair_validation",
        "regression_artifact",
        "activation_check",
    ],
)
def test_missing_provenance_fails_loading(committed, field):
    library, _ = committed
    ref = getattr(load_library(library).entries[0].provenance, field)
    ref.resolve(library.parent).unlink()
    with pytest.raises(FileNotFoundError):
        load_library(library)


def test_changed_evidence_fails_loading_even_after_rollback(committed):
    library, _ = committed
    rollback_control(library, REFUND_WINDOW_CONTROL_ID, "restore baseline")
    entry = load_library(library).entries[0]
    ref = next(r for r in entry.provenance.evidence if r.path.endswith(names.TRACE))
    path = ref.resolve(library.parent)
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="evidence changed"):
        load_library(library)


def test_omitting_a_required_evidence_reference_fails_loading(committed):
    library, _ = committed
    _edit(
        library,
        lambda p: p["entries"][0]["provenance"].update(
            evidence=[
                ref
                for ref in p["entries"][0]["provenance"]["evidence"]
                if not ref["path"].endswith(names.TRACE)
            ]
        ),
    )
    with pytest.raises(ValueError, match="missing retained trace"):
        load_library(library)


def test_library_is_portable_and_independent_of_temporary_runs(committed, tmp_path):
    library, _ = committed
    moved = tmp_path / "relocated"
    shutil.copytree(library.parent, moved)
    shutil.rmtree(tmp_path / "source")
    shutil.rmtree(tmp_path / "replay")
    shutil.rmtree(library.parent)
    assert (
        load_library(moved / "library.json").active_controls()[0].control_id
        == REFUND_WINDOW_CONTROL_ID
    )


def test_wrong_provenance_identity_is_rejected_even_with_matching_hash(committed):
    library, _ = committed
    entry = load_library(library).entries[0]
    ref = entry.provenance.repair_package
    path = ref.resolve(library.parent)
    _edit(path, lambda p: p.update(run_id="unrelated_run"))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()

    def change_refs(doc):
        provenance = doc["entries"][0]["provenance"]
        for value in [v for v in provenance.values() if isinstance(v, dict)] + provenance[
            "evidence"
        ]:
            if value["path"] == ref.path:
                value["sha256"] = digest

    _edit(library, change_refs)
    with pytest.raises(ValueError, match="mismatched originating"):
        load_library(library)


def test_invalid_library_fails_suite_before_execution(tmp_path):
    path = tmp_path / "library.json"
    path.write_text('{"schema_version": "999", "entries": []}')
    output = tmp_path / "runs"
    assert (
        main(["--runs-dir", str(output), "run-suite", str(SUITE), "--control-library", str(path)])
        == 2
    )
    assert not output.exists()


def test_concurrent_writer_is_rejected(committed):
    library, _ = committed
    before = library.read_bytes()
    library.with_name("library.json.lock").write_text("another writer")
    with pytest.raises(ValueError, match="locked"):
        rollback_control(library, REFUND_WINDOW_CONTROL_ID, "test")
    assert library.read_bytes() == before


@pytest.mark.parametrize("failure_point", ["fsync", "replace"])
def test_failed_manifest_write_preserves_library_and_cleans_new_evidence(
    tmp_path, monkeypatch, failure_point
):
    from trace_harness.environment import control_library as library_module

    artifact = _bundle(tmp_path)
    library = tmp_path / "controls/library.json"
    library.parent.mkdir()
    library.write_text(ControlLibrary().model_dump_json())
    before = library.read_bytes()

    def fail_write_operation(*args):
        raise OSError("simulated disk failure")

    original = library_module.write_library

    def fail_write(path, value):
        with monkeypatch.context() as patch:
            patch.setattr(library_module.os, failure_point, fail_write_operation)
            original(path, value)

    monkeypatch.setattr("trace_harness.regression.promotion.write_library", fail_write)
    with pytest.raises(OSError, match="disk failure"):
        _commit(tmp_path, artifact, library)
    assert library.read_bytes() == before
    assert list((library.parent / "evidence").iterdir()) == []
    assert not library.with_name("library.json.lock").exists()
    assert [p.name for p in library.parent.iterdir() if p.is_file()] == ["library.json"]


def test_commit_requires_explicit_validation_and_source_bundle(tmp_path):
    artifact = _bundle(tmp_path)
    library = tmp_path / "controls/library.json"
    assert main(["replay", str(artifact), "--commit", "--control-library", str(library)]) == 2
    artifact.with_name(names.REPAIR_PACKAGE).unlink()
    assert _commit(tmp_path, artifact, library) == 2
    assert not library.exists()


def test_removed_implementation_can_still_be_rolled_back(committed, monkeypatch):
    library, _ = committed
    monkeypatch.delitem(GUARDRAIL_REGISTRY, "unauthorized_cash_refund_guardrail")
    with pytest.raises(ValueError, match="unknown guardrail"):
        load_library(library)
    rollback_control(library, REFUND_WINDOW_CONTROL_ID, "implementation retired")
    assert load_library(library).active_controls() == []


def test_no_accepted_control_cannot_be_committed(tmp_path):
    artifact = _bundle(tmp_path)
    _edit(artifact.with_name(names.REPAIR_PACKAGE), lambda p: p.update(controls=[]))
    library = tmp_path / "controls/library.json"
    assert _commit(tmp_path, artifact, library) == 1
    assert not library.exists()


def _install_reference_set(monkeypatch, controls):
    monkeypatch.setattr(controls_module, "reference_controls", lambda: controls)
    monkeypatch.setattr(cli, "reference_controls", lambda: controls)


def test_environment_application_order_is_deterministic(tmp_path, monkeypatch):
    artifact = _bundle(tmp_path)
    original = reference_controls()[0]
    original.control_id = "z_control"
    second = original.model_copy(deep=True)
    second.control_id = "a_control"
    second.provenance.repair_control = "second_refund_control"
    _install_reference_set(monkeypatch, [original, second])
    _edit(
        artifact.with_name(names.REPAIR_PACKAGE),
        lambda p: p["controls"].append(
            {
                **p["controls"][0],
                "name": "second_refund_control",
            }
        ),
    )
    library = tmp_path / "controls/library.json"
    assert _commit(tmp_path, artifact, library) == 0
    _edit(library, lambda p: p["entries"].reverse())
    first = SupportEnvironment.from_task(load_task(DEMO), control_library=library)
    second = SupportEnvironment.from_task(load_task(DEMO), control_library=library)
    assert [c.control_id for c in first.installed_controls] == ["a_control", "z_control"]
    assert first.installed_controls == second.installed_controls


def test_second_promotion_appends_history_and_respects_selection(committed, tmp_path, monkeypatch):
    library, artifact = committed
    original_entry = load_library(library).entries[0]
    original = reference_controls()[0]
    additional = original.model_copy(deep=True)
    additional.control_id = "ctl_additional"
    additional.provenance.repair_control = "additional_refund_control"
    _install_reference_set(monkeypatch, [original, additional])
    _edit(
        artifact.with_name(names.REPAIR_PACKAGE),
        lambda p: p["controls"].append(
            {
                **p["controls"][0],
                "name": additional.provenance.repair_control,
            }
        ),
    )
    assert _commit(tmp_path, artifact, library, "--control", additional.control_id) == 0
    result = load_library(library)
    assert result.entries[0] == original_entry
    assert len(result.entries) == 2
    entry = result.entries[1]
    validation = json.loads(entry.provenance.repair_validation.read(library.parent))
    unselected = next(
        c for c in validation["controls"] if c["control"] == original.provenance.repair_control
    )
    assert unselected["verdict"] == "skipped"
    assert "not_selected" in unselected["reason"]
    gate = json.loads(entry.provenance.activation_check.read(library.parent))
    assert len(gate["regressions"]) == 2
    assert [c["control_id"] for c in gate["controls"]] == [
        additional.control_id,
        original.control_id,
    ]


def test_new_control_must_preserve_existing_library_regressions(committed, tmp_path, monkeypatch):
    library, _ = committed
    before = library.read_bytes()
    bundles = set((library.parent / "evidence").iterdir())
    next_dir = tmp_path / "next"
    artifact = _bundle(next_dir, sibling=False)
    control = reference_controls()[0].model_copy(deep=True)
    control.control_id = "ctl_overbroad"
    control.guardrail_ref = "block_every_refund"
    control.rule_ref = RuleRef(source="none")
    control.provenance.repair_control = "overbroad_refund_control"

    def block_every_refund(call, state):
        if call.tool_name == "issue_refund":
            return ToolResult(tool_name=call.tool_name, status="error", error="blocked")
        return None

    monkeypatch.setitem(
        GUARDRAIL_REGISTRY,
        control.guardrail_ref,
        RegisteredGuardrail(
            fn=block_every_refund,
            rule_source="none",
            rule_keys=frozenset(),
        ),
    )
    _install_reference_set(monkeypatch, [control])
    _edit(
        artifact.with_name(names.REPAIR_PACKAGE),
        lambda p: p["controls"][0].update(
            name=control.provenance.repair_control,
        ),
    )
    assert _commit(next_dir, artifact, library) == 1
    assert library.read_bytes() == before
    assert set((library.parent / "evidence").iterdir()) == bundles
    verdict = json.loads(next((next_dir / "replay").rglob(names.REPAIR_VALIDATION)).read_text())
    assert verdict["rollup"]["accepted"] == 1  # rejection came from the existing sibling


def _outcomes(store, summary):
    outcomes = {}
    for entry in summary.entries:
        verifier = store.read_json(entry.run_id, names.VERIFIER_RESULT)
        outcomes[entry.task_id] = {
            "verdict": verifier["verdict"],
            "failed_check_ids": sorted(c["check_id"] for c in verifier["failed_checks"]),
            "severity": verifier["severity"],
            "blocks_release": verifier["blocks_release"],
        }
    return outcomes


def test_full_suite_preserves_positive_cases_and_rollback_restores_baseline(
    committed, tmp_path, capsys
):
    library, _ = committed
    suite = load_suite(SUITE)
    baseline_store = ArtifactStore(tmp_path / "baseline")
    baseline = BatchRunner(baseline_store).run(suite)
    active_store = ArtifactStore(tmp_path / "active")
    assert (
        main(
            [
                "--runs-dir",
                str(active_store.runs_dir),
                "run-suite",
                str(SUITE),
                "--control-library",
                str(library),
            ]
        )
        == 0
    )
    assert "1 active (0 gating, 1 advisory) installed" in capsys.readouterr().out
    active = BatchSummary.model_validate_json(
        next(active_store.runs_dir.glob("batches/*/batch_summary.json")).read_bytes()
    )
    before, after = _outcomes(baseline_store, baseline), _outcomes(active_store, active)
    assert len(before) == len(after) == 32
    passing = {task for task, result in before.items() if result["verdict"] == "pass"}
    assert len(passing) == 18
    assert all(after[task] == before[task] for task in passing)
    assert "unauthorized_cash_refund" in before["refund_policy_failure"]["failed_check_ids"]
    assert "unauthorized_cash_refund" not in after["refund_policy_failure"]["failed_check_ids"]
    expected = json.loads((FIXTURES_DIR / "controls/refund_v0_expected.json").read_text())
    assert after == expected["tasks"]

    rollback_control(library, REFUND_WINDOW_CONTROL_ID, "restore suite baseline")
    rolled_store = ArtifactStore(tmp_path / "rolled-back")
    rolled = BatchRunner(rolled_store, control_library=library).run(suite)
    assert _outcomes(rolled_store, rolled) == before
    for old, restored in zip(baseline.entries, rolled.entries, strict=True):
        assert baseline_store.read_json(old.run_id, names.FINAL_STATE) == rolled_store.read_json(
            restored.run_id,
            names.FINAL_STATE,
        )


@pytest.mark.parametrize("change", ["version", "duplicate", "history", "escape"])
def test_invalid_library_contract_is_rejected(committed, change):
    library, _ = committed

    def mutate(doc):
        if change == "version":
            doc["schema_version"] = "2.0.0"
        elif change == "duplicate":
            doc["entries"].append(doc["entries"][0])
        elif change == "history":
            doc["entries"][0]["status"] = "rolled_back"
        else:
            doc["entries"][0]["provenance"]["source_run"]["path"] = "../outside.json"

    _edit(library, mutate)
    with pytest.raises(ValueError):
        load_library(library)


def test_committed_example_loads_and_matches_controlled_pins():
    path = FIXTURES_DIR / "controls/library.json"
    library = load_library(path)
    expected = json.loads((path.parent / "refund_v0_expected.json").read_text())
    assert [c.control_id for c in library.active_controls()] == expected["control_ids"]
    assert library.entries[0].provenance.repair_validation.path == expected["validation"]


def test_empty_library_is_valid():
    assert ControlLibrary().active_controls() == []


# --- acceptance basis (ADR-0002, decision 2) ---


def _set_acceptance(library, replay_mode, standing, predicted_by="heuristic_v1"):
    _edit(
        library,
        lambda p: p["entries"][0].update(
            acceptance={
                "replay_mode": replay_mode,
                "predicted_by": predicted_by,
                "standing": standing,
            }
        ),
    )


def test_committed_library_entry_reads_as_advisory():
    """ctl_refund_window_v1 predates the field, so its basis reads as not recorded."""
    raw = COMMITTED_LIBRARY.read_bytes()
    assert "acceptance" not in json.loads(raw)["entries"][0]
    library = load_library(COMMITTED_LIBRARY)
    assert library.schema_version == "0.1.0"
    (entry,) = [e for e in library.entries if e.control.control_id == REFUND_WINDOW_CONTROL_ID]
    assert entry.acceptance == AcceptanceBasis(replay_mode=None, standing="advisory")
    assert COMMITTED_LIBRARY.read_bytes() == raw


@pytest.fixture
def classified_static_ok(monkeypatch):
    """Widen the refund guardrail's declared coverage so the classifier itself says static_ok.

    The shipped guardrail covers only unauthorized_cash_refund while issue_refund
    can also reach unauthorized_store_credit, which is why every real artifact
    is live_required. Covering both satisfies all four rules honestly.
    """
    ref = "unauthorized_cash_refund_guardrail"
    monkeypatch.setitem(
        GUARDRAIL_REGISTRY,
        ref,
        dataclasses.replace(
            GUARDRAIL_REGISTRY[ref],
            checks_covered=frozenset({"unauthorized_cash_refund", "unauthorized_store_credit"}),
        ),
    )


def test_a_classified_static_ok_label_commits_as_gating(tmp_path, classified_static_ok, capsys):
    artifact = _bundle(tmp_path)
    labeled = json.loads(artifact.read_text())
    assert labeled["replay_mode"] == "static_ok"
    assert labeled["replay_mode_basis"]["predicted_by"] == "heuristic_v1"
    library = tmp_path / "controls/library.json"
    assert _commit(tmp_path, artifact, library) == 0
    (entry,) = load_library(library).entries
    assert entry.acceptance == AcceptanceBasis(
        replay_mode="static_ok", predicted_by="heuristic_v1", standing="gating"
    )
    capsys.readouterr()
    assert main(["controls", "list", "--control-library", str(library)]) == 0
    out = capsys.readouterr().out
    assert "gating (replay_mode static_ok, predicted until #159 measures it)" in out
    assert "1 gating on a predicted label until #159 measures it" in out


def test_an_artifact_that_supports_gating_cannot_be_recorded_as_advisory(
    tmp_path, classified_static_ok
):
    artifact = _bundle(tmp_path)
    library = tmp_path / "controls/library.json"
    assert _commit(tmp_path, artifact, library) == 0
    _set_acceptance(library, "static_ok", "advisory")
    with pytest.raises(ValueError, match="supports gating"):
        load_library(library)


def test_a_basis_naming_another_predictor_is_refused(tmp_path, classified_static_ok):
    artifact = _bundle(tmp_path)
    library = tmp_path / "controls/library.json"
    assert _commit(tmp_path, artifact, library) == 0
    _set_acceptance(library, "static_ok", "gating", predicted_by="measured")
    with pytest.raises(ValueError, match="records a label from measured"):
        load_library(library)


@pytest.mark.parametrize(
    ("basis", "predicted_by", "refusal"),
    [
        ("kept", "heuristic_v1", "classifies as live_required"),
        ("removed", None, "has no recorded basis"),
    ],
)
def test_a_static_ok_label_its_basis_does_not_support_is_advisory(
    tmp_path, basis, predicted_by, refusal
):
    """A hand-set static_ok commits as advisory, and a gating claim on it fails to load."""
    artifact = _bundle(tmp_path)

    def relabel(a):
        a["replay_mode"] = "static_ok"
        if basis == "removed":
            a["replay_mode_basis"] = None

    _edit(artifact, relabel)
    library = tmp_path / "controls/library.json"
    assert _commit(tmp_path, artifact, library) == 0
    (entry,) = load_library(library).entries
    assert entry.acceptance == AcceptanceBasis(
        replay_mode="static_ok", predicted_by=predicted_by, standing="advisory"
    )
    _set_acceptance(library, "static_ok", "gating", predicted_by=predicted_by)
    with pytest.raises(ValueError, match=refusal):
        load_library(library)


def test_gating_acceptance_on_an_advisory_artifact_is_refused(committed):
    library, _ = committed
    _set_acceptance(library, "live_required", "gating")
    with pytest.raises(ValueError, match="gating acceptance but the artifact is live_required"):
        load_library(library)


def test_acceptance_basis_must_name_the_artifact_replay_mode(committed):
    library, _ = committed
    _set_acceptance(library, "unlabeled", "advisory")
    with pytest.raises(ValueError, match="records replay_mode unlabeled"):
        load_library(library)


def test_a_basis_without_a_replay_mode_cannot_be_gating(committed):
    library, _ = committed
    _set_acceptance(library, None, "gating")
    with pytest.raises(ValueError, match="without the replay_mode it relied on"):
        load_library(library)


@pytest.mark.parametrize(
    ("field", "value", "refusal"),
    [
        ("replay_mode", "static_ok", "validated as static_ok"),
        ("predicted_by", "measured", "validated on a label from measured"),
    ],
)
def test_validation_label_must_match_the_artifact(committed, field, value, refusal):
    library, _ = committed
    root = library.parent
    (entry,) = load_library(library).entries
    refs = entry.provenance
    validation = RepairValidation.model_validate_json(refs.repair_validation.read(root))
    for control in validation.controls:
        setattr(control, field, value)
    with pytest.raises(ValueError, match=refusal):
        check_acceptance(
            entry.control,
            RunResult.model_validate_json(refs.source_run.read(root)),
            RepairPackage.model_validate_json(refs.repair_package.read(root)),
            RegressionArtifact.model_validate_json(refs.regression_artifact.read(root)),
            validation,
            entry.acceptance,
        )


def test_controls_list_shows_each_acceptance_basis(capsys):
    assert main(["controls", "list", "--control-library", str(COMMITTED_LIBRARY)]) == 0
    out = capsys.readouterr().out
    assert "schema 0.1.0" in out
    assert REFUND_WINDOW_CONTROL_ID in out
    assert "active · advisory (replay_mode not recorded)" in out
    assert "1 active (0 gating, 1 advisory)" in out


def test_writing_an_old_library_records_its_basis_explicitly(tmp_path):
    """A rollback rewrites the manifest at the current schema without changing its meaning."""
    copy = tmp_path / "controls"
    shutil.copytree(COMMITTED_LIBRARY.parent, copy)
    rollback_control(copy / "library.json", REFUND_WINDOW_CONTROL_ID, "exercise the migration")
    written = json.loads((copy / "library.json").read_text())
    assert written["schema_version"] == CONTROL_LIBRARY_SCHEMA_VERSION
    assert written["entries"][0]["acceptance"] == {
        "replay_mode": None,
        "predicted_by": None,
        "standing": "advisory",
    }
    assert load_library(copy / "library.json").active_controls() == []


def _as_written_by_main(library):
    """Reshape a committed library the way main writes one.

    Main labels every artifact, so the retained artifact stays live_required,
    but it writes repair_validation.json at 0.1.0 with no replay_mode and an
    entry with no acceptance basis, at library schema 0.1.0.
    """
    doc = json.loads(library.read_text())
    (entry,) = doc["entries"]
    ref = entry["provenance"]["repair_validation"]
    path = library.parent / ref["path"]
    validation = json.loads(path.read_text())
    validation["schema_version"] = "0.1.0"
    for control in validation["controls"]:
        for key in ("replay_mode", "standing", "predicted_by"):
            control.pop(key, None)
        for rerun in [control["originating_rerun"], *control["sibling_reruns"]]:
            if rerun is not None:
                rerun.pop("task_fixture", None)
    validation["rollup"] = {k: validation["rollup"][k] for k in ("accepted", "rejected", "skipped")}
    path.write_text(json.dumps(validation, indent=2) + "\n")
    ref["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    del entry["acceptance"]
    doc["schema_version"] = "0.1.0"
    library.write_text(json.dumps(doc, indent=2) + "\n")
    artifact = json.loads(
        (library.parent / entry["provenance"]["regression_artifact"]["path"]).read_text()
    )
    return artifact


def test_a_library_written_by_main_loads_as_advisory_and_rolls_back(committed, capsys):
    """A labeled artifact beside an unlabeled validation is not a contradiction."""
    library, _ = committed
    artifact = _as_written_by_main(library)
    assert artifact["replay_mode"] == "live_required"
    (entry,) = load_library(library).entries
    assert entry.acceptance == AcceptanceBasis(replay_mode=None, standing="advisory")
    assert main(["controls", "list", "--control-library", str(library)]) == 0
    assert "advisory (replay_mode not recorded)" in capsys.readouterr().out
    rollback_control(library, REFUND_WINDOW_CONTROL_ID, "exercise a main-written library")
    (entry,) = load_library(library, resolve_active=False).entries
    assert entry.status == "rolled_back"
    assert entry.acceptance.standing == "advisory"
