"""Per-control validation verdicts (issue #146).

Covers real accepted, ineffective, overblocking, and interrupted replays,
control selection, prescription provenance, inspection, CI exit behavior, and
the replay_mode standing each verdict carries.
"""

from __future__ import annotations

import json

import pytest

from conftest import FIXTURES_DIR
from trace_harness import cli
from trace_harness.cli import main
from trace_harness.environment import controls as controls_module
from trace_harness.environment.controls import (
    GUARDRAIL_REGISTRY,
    REFUND_WINDOW_CONTROL_ID,
    RegisteredGuardrail,
    reference_controls,
)
from trace_harness.environment.tools import ToolResult
from trace_harness.regression.repair_validation import (
    REPAIR_VALIDATION_SCHEMA_VERSION,
    ControlValidation,
    ControlVerdict,
    RepairValidation,
    decide_verdict,
    skipped_control,
)
from trace_harness.tracing import artifact_store as names
from trace_harness.tracing.artifact_store import ArtifactStore

CONTROL_DEMO_TASK_PATH = FIXTURES_DIR / "tasks" / "refund_policy_control_demo.json"
PINNED_VALIDATION = (
    FIXTURES_DIR
    / "controls"
    / "evidence"
    / "4f23ca45a8a047758b3dd1f6adf9b732"
    / names.REPAIR_VALIDATION
)


def _bundle_artifact(tmp_path):
    runs_dir = tmp_path / "runs_bundle"
    assert main(["--runs-dir", str(runs_dir), "run-pipeline", str(CONTROL_DEMO_TASK_PATH)]) == 0
    (run_dir,) = [p for p in runs_dir.iterdir() if p.is_dir()]
    return run_dir / names.REGRESSION_ARTIFACT


def _validation_for(tmp_path, extra_args=()):
    """Replay the demo bundle with controls and return the written artifact."""
    artifact = _bundle_artifact(tmp_path)
    return _replay_validation(tmp_path, artifact, extra_args)


def _replay_validation(tmp_path, artifact, extra_args=()):
    replay_dir = tmp_path / "runs_replay"
    code = main(
        ["--runs-dir", str(replay_dir), "replay", str(artifact), "--apply-control", *extra_args]
    )
    source_run_id = json.loads(artifact.read_text())["source_run_id"]
    data = json.loads(
        (replay_dir / source_run_id / names.REPAIR_VALIDATION).read_text(encoding="utf-8")
    )
    return code, RepairValidation.model_validate(data)


def _edit_json(path, edit):
    data = json.loads(path.read_text())
    edit(data)
    path.write_text(json.dumps(data))


def _replace_refund_guardrail(monkeypatch, fn):
    ref = "unauthorized_cash_refund_guardrail"
    original = GUARDRAIL_REGISTRY[ref]
    monkeypatch.setitem(
        GUARDRAIL_REGISTRY,
        ref,
        RegisteredGuardrail(fn=fn, rule_source=original.rule_source, rule_keys=original.rule_keys),
    )


def test_prescriptions_come_from_input_bundle_in_a_separate_directory(tmp_path):
    artifact = _bundle_artifact(tmp_path)
    package_path = artifact.with_name(names.REPAIR_PACKAGE)
    _edit_json(package_path, lambda p: p["controls"][-1].update(name="future_control"))
    package = json.loads(package_path.read_text())

    code, validation = _replay_validation(tmp_path, artifact)

    assert code == 0
    assert validation.controls_source == "repair_package"
    assert [c.control for c in validation.controls] == [c["name"] for c in package["controls"]]
    assert validation.controls[-1].verdict is ControlVerdict.SKIPPED


def test_empty_package_does_not_invent_prescriptions(tmp_path):
    artifact = _bundle_artifact(tmp_path)
    _edit_json(artifact.with_name(names.REPAIR_PACKAGE), lambda p: p.update(controls=[]))
    code, validation = _replay_validation(tmp_path, artifact)
    assert code == 0
    assert validation.controls_source == "repair_package"
    assert validation.controls == []


@pytest.mark.parametrize("change", ["malformed", "wrong_run", "wrong_task", "unlinked_check"])
def test_invalid_package_fails_before_replay(tmp_path, capsys, change):
    artifact = _bundle_artifact(tmp_path)
    package = artifact.with_name(names.REPAIR_PACKAGE)
    if change == "malformed":
        package.write_text("{")
    elif change == "wrong_run":
        _edit_json(package, lambda p: p.update(run_id="another_run"))
    elif change == "wrong_task":
        _edit_json(package, lambda p: p.update(task_id="another_task"))
    else:
        _edit_json(package, lambda p: p["controls"][0].update(linked_verifier_checks=["unknown"]))
    output = tmp_path / "replay"
    capsys.readouterr()
    assert main(["--runs-dir", str(output), "replay", str(artifact), "--apply-control"]) == 2
    assert capsys.readouterr().err
    assert not output.exists() or not list(output.iterdir())


def test_standalone_artifact_records_reference_control_fallback(tmp_path):
    artifact = _bundle_artifact(tmp_path)
    standalone = tmp_path / "standalone.json"
    standalone.write_text(artifact.read_text())
    code, validation = _replay_validation(tmp_path, standalone)
    assert code == 0
    assert validation.controls_source == "reference_controls"
    assert [c.control_id for c in validation.controls] == [REFUND_WINDOW_CONTROL_ID]


def test_control_selection_is_respected_during_individual_validation(tmp_path, monkeypatch):
    artifact = _bundle_artifact(tmp_path)
    original = reference_controls()[0]
    unselected = original.model_copy(deep=True)
    unselected.control_id = "ctl_unselected"
    unselected.provenance.repair_control = "future_control"

    def available():
        return [original, unselected]

    monkeypatch.setattr(controls_module, "reference_controls", available)
    monkeypatch.setattr(cli, "reference_controls", available)
    _edit_json(
        artifact.with_name(names.REPAIR_PACKAGE),
        lambda p: p["controls"][-1].update(name="future_control"),
    )

    code, validation = _replay_validation(
        tmp_path, artifact, ("--control", REFUND_WINDOW_CONTROL_ID)
    )
    assert code == 0
    unselected_result = next(c for c in validation.controls if c.control == "future_control")
    assert unselected_result.verdict is ControlVerdict.SKIPPED
    assert "not_selected" in unselected_result.reason
    assert unselected_result.originating_rerun is None
    assert len(ArtifactStore(tmp_path / "runs_replay").read_index().entries) == 2


@pytest.mark.parametrize("gated", [False, True])
def test_individual_rejection_gates_an_otherwise_passing_bundle(
    tmp_path, monkeypatch, capsys, gated
):
    artifact = _bundle_artifact(tmp_path)
    original = reference_controls()[0]
    ineffective = original.model_copy(deep=True)
    ineffective.control_id = "ctl_ineffective"
    ineffective.guardrail_ref = "ineffective"
    ineffective.provenance.repair_control = "ineffective_control"
    registered = GUARDRAIL_REGISTRY[original.guardrail_ref]
    monkeypatch.setitem(
        GUARDRAIL_REGISTRY,
        ineffective.guardrail_ref,
        RegisteredGuardrail(
            fn=lambda call, state: None,
            rule_source=registered.rule_source,
            rule_keys=registered.rule_keys,
        ),
    )

    def available():
        return [original, ineffective]

    monkeypatch.setattr(controls_module, "reference_controls", available)
    monkeypatch.setattr(cli, "reference_controls", available)
    _edit_json(
        artifact.with_name(names.REPAIR_PACKAGE),
        lambda p: p["controls"].append({**p["controls"][0], "name": "ineffective_control"}),
    )
    capsys.readouterr()
    code, validation = _replay_validation(
        tmp_path, artifact, ("--fail-on-rejected",) if gated else ()
    )
    assert validation.rollup.accepted == 1
    assert validation.rollup.rejected == 1
    assert code == (1 if gated else 0)
    assert f"Replay result: {'FAIL' if gated else 'PASS'}" in capsys.readouterr().out


def test_only_linked_checks_are_counted_as_cleared(tmp_path):
    runs_dir = tmp_path / "bundle"
    task = FIXTURES_DIR / "tasks" / "refund_policy_failure.json"
    assert main(["--runs-dir", str(runs_dir), "run-pipeline", str(task)]) == 0
    (run_dir,) = [p for p in runs_dir.iterdir() if p.is_dir()]
    code, validation = _replay_validation(tmp_path, run_dir / names.REGRESSION_ARTIFACT)
    assert code == 1
    control = next(c for c in validation.controls if c.control_id == REFUND_WINDOW_CONTROL_ID)
    assert control.originating_rerun.cleared_checks == ["unauthorized_cash_refund"]
    assert "required_escalation_missing" in control.originating_rerun.failed_checks
    assert "final_answer_inconsistent_with_state" in control.originating_rerun.failed_checks
    assert control.verdict is ControlVerdict.REJECTED_OVERBLOCKS
    assert "never pinned" in control.reason


def test_materializable_control_without_linked_checks_is_skipped(tmp_path):
    artifact = _bundle_artifact(tmp_path)
    _edit_json(
        artifact.with_name(names.REPAIR_PACKAGE),
        lambda p: p["controls"][0].update(linked_verifier_checks=[]),
    )
    _, validation = _replay_validation(tmp_path, artifact)
    control = validation.controls[0]
    assert control.verdict is ControlVerdict.SKIPPED
    assert "no_linked_checks" in control.reason
    assert control.originating_rerun is None


def test_incomplete_pinned_run_cannot_earn_acceptance(tmp_path, capsys):
    artifact = _bundle_artifact(tmp_path)
    _edit_json(artifact, lambda a: a.update(pinned_agent_actions=a["pinned_agent_actions"][:-1]))
    capsys.readouterr()
    code, validation = _replay_validation(tmp_path, artifact)
    assert code == 1
    control = validation.controls[0]
    assert control.verdict is ControlVerdict.SKIPPED
    assert "validation_incomplete" in control.reason
    assert control.originating_rerun.verdict == "INCOMPLETE"
    assert control.originating_rerun.cleared_checks == []
    assert "Replay result: FAIL" in capsys.readouterr().out


def test_incomplete_isolated_run_fails_even_when_bundle_completes(tmp_path, monkeypatch, capsys):
    artifact = _bundle_artifact(tmp_path)
    real_run = cli._run_fixture
    calls = 0

    def interrupt_individual(args, store, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            args.max_steps = 2
        return real_run(args, store, **kwargs)

    monkeypatch.setattr(cli, "_run_fixture", interrupt_individual)
    capsys.readouterr()
    code, validation = _replay_validation(tmp_path, artifact)
    assert code == 1
    assert validation.has_incomplete
    assert validation.rollup.accepted == 0
    assert "Replay result: FAIL" in capsys.readouterr().out


def test_incomplete_sibling_is_distinguished_from_overblocking(tmp_path, monkeypatch):
    artifact = _bundle_artifact(tmp_path)
    sibling_path = FIXTURES_DIR / "tasks" / "refund_policy_valid_cash.json"
    _edit_json(
        artifact,
        lambda a: a.update(
            positive_sibling_tests=[
                {
                    "test_name": "valid_cash_refund",
                    "task_fixture": str(sibling_path),
                }
            ]
        ),
    )
    real_run = cli._run_fixture

    def interrupt_sibling(args, store, **kwargs):
        if args.task_path == str(sibling_path):
            args.max_steps = 1
        return real_run(args, store, **kwargs)

    monkeypatch.setattr(cli, "_run_fixture", interrupt_sibling)
    code, validation = _replay_validation(tmp_path, artifact)
    assert code == 1
    control = validation.controls[0]
    assert control.verdict is ControlVerdict.SKIPPED
    assert "validation_incomplete" in control.reason
    assert control.originating_rerun.verdict == "PASS"
    assert control.sibling_reruns[0].verdict == "INCOMPLETE"


def test_incomplete_plain_replay_cannot_confirm_regression(tmp_path, capsys):
    artifact = _bundle_artifact(tmp_path)
    _edit_json(artifact, lambda a: a.update(pinned_agent_actions=a["pinned_agent_actions"][:-1]))
    capsys.readouterr()
    assert main(["--runs-dir", str(tmp_path / "replay"), "replay", str(artifact)]) == 1
    assert "Replay result: FAIL" in capsys.readouterr().out


def test_inspect_validation_in_separate_output_directory(tmp_path, capsys):
    _, validation = _validation_for(tmp_path)
    capsys.readouterr()
    code = main(["--runs-dir", str(tmp_path / "runs_replay"), "inspect", validation.run_id])
    captured = capsys.readouterr()
    assert code == 0
    assert "accepted" in captured.out
    assert "deterministic_pre_call_refund_guardrail [advisory]" in captured.out
    assert "1 accepted (0 gating, 1 advisory)" in captured.out
    assert not captured.err


def test_ineffective_control_is_rejected_with_real_evidence(tmp_path, monkeypatch):
    artifact = _bundle_artifact(tmp_path)
    _replace_refund_guardrail(monkeypatch, lambda call, state: None)
    code, validation = _replay_validation(tmp_path, artifact, ("--fail-on-rejected",))
    assert code == 1
    control = validation.controls[0]
    assert control.verdict is ControlVerdict.REJECTED_FAILURE_PERSISTS
    assert control.originating_rerun.failed_checks == ["unauthorized_cash_refund"]
    assert control.originating_rerun.cleared_checks == []


def test_overbroad_control_is_rejected_by_real_sibling(tmp_path, monkeypatch):
    artifact = _bundle_artifact(tmp_path)
    _edit_json(
        artifact,
        lambda a: a.update(
            positive_sibling_tests=[
                {
                    "test_name": "valid_cash_refund",
                    "task_fixture": str(FIXTURES_DIR / "tasks" / "refund_policy_valid_cash.json"),
                }
            ]
        ),
    )

    def block_refunds(call, state):
        if call.tool_name == "issue_refund":
            return ToolResult(tool_name=call.tool_name, status="error", error="blocked")
        return None

    _replace_refund_guardrail(monkeypatch, block_refunds)
    code, validation = _replay_validation(tmp_path, artifact, ("--fail-on-rejected",))
    assert code == 1
    control = validation.controls[0]
    assert control.verdict is ControlVerdict.REJECTED_OVERBLOCKS
    assert control.originating_rerun.verdict == "PASS"
    assert "valid_cash_refund" in control.reason
    sibling = control.sibling_reruns[0]
    assert sibling.verdict == "FAIL"
    assert "expected_refund_missing" in sibling.failed_checks
    store = ArtifactStore(tmp_path / "runs_replay")
    assert {e.run_id for e in store.read_index().entries if e.batch_id == validation.batch_id} == {
        control.originating_rerun.run_id,
        sibling.run_id,
    }


# --- the accepted path, end to end ---


@pytest.mark.parametrize("with_sibling", [False, True])
def test_refund_guardrail_is_accepted_with_rerun_evidence(tmp_path, with_sibling) -> None:
    artifact = _bundle_artifact(tmp_path)
    if with_sibling:
        _edit_json(
            artifact,
            lambda a: a.update(
                positive_sibling_tests=[
                    {
                        "test_name": "valid_cash_refund",
                        "task_fixture": str(
                            FIXTURES_DIR / "tasks" / "refund_policy_valid_cash.json"
                        ),
                    }
                ]
            ),
        )
    code, validation = _replay_validation(tmp_path, artifact)

    assert code == 0
    assert validation.schema_version == REPAIR_VALIDATION_SCHEMA_VERSION
    accepted = [c for c in validation.controls if c.verdict is ControlVerdict.ACCEPTED]
    assert [c.control for c in accepted] == ["deterministic_pre_call_refund_guardrail"]

    (control,) = accepted
    assert control.control_id == REFUND_WINDOW_CONTROL_ID
    assert control.guardrail_ref == "unauthorized_cash_refund_guardrail"
    assert control.originating_rerun is not None
    assert control.originating_rerun.cleared_checks == ["unauthorized_cash_refund"]
    assert len(control.sibling_reruns) == int(with_sibling)
    store = ArtifactStore(tmp_path / "runs_replay")
    for rerun in [control.originating_rerun, *control.sibling_reruns]:
        evidence = store.read_json(rerun.run_id, names.VERIFIER_RESULT)
        assert rerun.verdict == "PASS"
        assert evidence["verdict"] == "pass"
        assert evidence["failed_checks"] == rerun.failed_checks == []


def test_every_prescribed_control_gets_a_verdict(tmp_path) -> None:
    _, validation = _validation_for(tmp_path)

    assert validation.controls, "a repair package with no controls would prove nothing"
    assert all(c.verdict in set(ControlVerdict) for c in validation.controls)
    assert validation.rollup.accepted + validation.rollup.rejected + validation.rollup.skipped == (
        len(validation.controls)
    )


def test_unmaterializable_controls_are_skipped_not_pretended(tmp_path) -> None:
    """A prescribed control with no guardrail is reported, never counted as working."""
    _, validation = _validation_for(tmp_path)

    skipped = [c for c in validation.controls if c.verdict is ControlVerdict.SKIPPED]
    assert skipped, "the demo package prescribes controls nothing implements yet"
    for control in skipped:
        assert control.reason is not None
        assert "not_materializable" in control.reason
        assert control.originating_rerun is None  # nothing was run, nothing is claimed


def test_validation_reruns_are_grouped_into_one_batch(tmp_path) -> None:
    _, validation = _validation_for(tmp_path)
    assert validation.batch_id is not None

    replay_dir = tmp_path / "runs_replay"
    store = ArtifactStore(replay_dir)
    tagged = [e for e in store.read_index().entries if e.batch_id == validation.batch_id]
    assert tagged, "re-runs must be discoverable as one validation session"


# --- the rejection paths, decided directly ---


def test_failure_persists_when_pinned_checks_still_fire() -> None:
    verdict, reason = decide_verdict(
        expected_checks={"unauthorized_cash_refund"},
        pinned_failed_checks={"unauthorized_cash_refund"},
        pinned_introduced_blocking=set(),
        failing_siblings=[],
    )
    assert verdict is ControlVerdict.REJECTED_FAILURE_PERSISTS
    assert "still fired" in reason


def test_overblocks_when_a_positive_sibling_fails() -> None:
    verdict, reason = decide_verdict(
        expected_checks={"unauthorized_cash_refund"},
        pinned_failed_checks=set(),
        pinned_introduced_blocking=set(),
        failing_siblings=["valid_cash_refund_within_window"],
    )
    assert verdict is ControlVerdict.REJECTED_OVERBLOCKS
    assert "valid_cash_refund_within_window" in reason


def test_overblocks_when_the_control_introduces_a_new_blocking_check() -> None:
    """Trading one blocking failure for another is not a fix."""
    verdict, reason = decide_verdict(
        expected_checks={"unauthorized_cash_refund"},
        pinned_failed_checks={"required_escalation_missing"},
        pinned_introduced_blocking={"required_escalation_missing"},
        failing_siblings=[],
    )
    assert verdict is ControlVerdict.REJECTED_OVERBLOCKS
    assert "never pinned" in reason


def test_persisting_failure_outranks_overblocking() -> None:
    """A control that did not fix its own failure is rejected on that ground."""
    verdict, _ = decide_verdict(
        expected_checks={"unauthorized_cash_refund"},
        pinned_failed_checks={"unauthorized_cash_refund"},
        pinned_introduced_blocking=set(),
        failing_siblings=["valid_cash_refund_within_window"],
    )
    assert verdict is ControlVerdict.REJECTED_FAILURE_PERSISTS


# --- rollup and the CI gate ---


def test_rollup_is_recounted_from_the_controls() -> None:
    validation = RepairValidation(
        run_id="run_x",
        test_name="t",
        controls=[
            ControlValidation(control="a", verdict=ControlVerdict.ACCEPTED),
            ControlValidation(control="b", verdict=ControlVerdict.REJECTED_OVERBLOCKS),
            ControlValidation(control="c", verdict=ControlVerdict.REJECTED_FAILURE_PERSISTS),
            skipped_control("d"),
        ],
    ).rebuild_rollup()

    assert (validation.rollup.accepted, validation.rollup.rejected, validation.rollup.skipped) == (
        1,
        2,
        1,
    )
    assert validation.has_rejection


def test_reading_validation_recounts_stale_rollup():
    validation = RepairValidation.model_validate(
        {
            "run_id": "run_x",
            "test_name": "t",
            "controls": [{"control": "a", "verdict": "rejected_overblocks"}],
            "rollup": {"accepted": 1, "rejected": 0, "skipped": 0},
        }
    )
    assert validation.rollup.accepted == 0
    assert validation.rollup.rejected == 1
    assert validation.has_rejection


def test_a_clean_validation_has_no_rejection() -> None:
    validation = RepairValidation(
        run_id="run_x",
        test_name="t",
        controls=[
            ControlValidation(control="a", verdict=ControlVerdict.ACCEPTED),
            skipped_control("b"),
        ],
    ).rebuild_rollup()
    assert not validation.has_rejection  # skipped is not a rejection


def test_fail_on_rejected_requires_apply_control(tmp_path, capsys) -> None:
    artifact = _bundle_artifact(tmp_path)
    capsys.readouterr()
    code = main(["--runs-dir", str(tmp_path / "r"), "replay", str(artifact), "--fail-on-rejected"])
    assert code == 2
    assert "requires --apply-control" in capsys.readouterr().err


def test_fail_on_rejected_passes_when_nothing_is_rejected(tmp_path) -> None:
    code, validation = _validation_for(tmp_path, extra_args=("--fail-on-rejected",))
    assert validation.rollup.rejected == 0
    assert code == 0


# --- replay_mode standing (ADR-0002, decision 2) ---


@pytest.mark.parametrize(
    ("replay_mode", "standing"),
    [("static_ok", "gating"), ("live_required", "advisory"), (None, "advisory")],
)
def test_every_verdict_records_the_artifact_replay_mode(tmp_path, replay_mode, standing):
    artifact = _bundle_artifact(tmp_path)
    if replay_mode is None:
        _edit_json(artifact, lambda a: a.pop("replay_mode"))
    else:
        _edit_json(artifact, lambda a: a.update(replay_mode=replay_mode))

    code, validation = _replay_validation(tmp_path, artifact)

    assert code == 0
    expected_mode = replay_mode or "unlabeled"
    assert {c.replay_mode for c in validation.controls} == {expected_mode}
    (accepted,) = [c for c in validation.controls if c.verdict is ControlVerdict.ACCEPTED]
    assert accepted.standing == standing
    assert (validation.rollup.accepted_gating, validation.rollup.accepted_advisory) == (
        (1, 0) if standing == "gating" else (0, 1)
    )


def test_rollup_splits_accepted_verdicts_by_standing() -> None:
    validation = RepairValidation(
        run_id="run_x",
        test_name="t",
        controls=[
            ControlValidation(
                control="a", verdict=ControlVerdict.ACCEPTED, replay_mode="static_ok"
            ),
            ControlValidation(control="b", verdict=ControlVerdict.ACCEPTED),
            ControlValidation(
                control="c", verdict=ControlVerdict.ACCEPTED, replay_mode="live_required"
            ),
            ControlValidation(
                control="d", verdict=ControlVerdict.REJECTED_OVERBLOCKS, replay_mode="static_ok"
            ),
        ],
    )
    rollup = validation.rollup
    assert (rollup.accepted, rollup.accepted_gating, rollup.accepted_advisory) == (3, 1, 2)


def test_a_written_standing_is_derived_again_on_read() -> None:
    """An edited file must not promote an advisory verdict to gating."""
    control = ControlValidation.model_validate(
        {
            "control": "a",
            "verdict": "accepted",
            "replay_mode": "live_required",
            "standing": "gating",
        }
    )
    assert control.standing == "advisory"
    assert control.model_dump(mode="json")["standing"] == "advisory"


def test_pinned_validation_evidence_reads_as_advisory_and_unchanged() -> None:
    """The evidence behind ctl_refund_window_v1 predates the label and stays pinned."""
    raw = PINNED_VALIDATION.read_bytes()
    validation = RepairValidation.model_validate_json(raw)
    assert validation.schema_version == "0.1.0"
    assert {c.replay_mode for c in validation.controls} == {"unlabeled"}
    (accepted,) = [c for c in validation.controls if c.verdict is ControlVerdict.ACCEPTED]
    assert accepted.control_id == REFUND_WINDOW_CONTROL_ID
    assert accepted.standing == "advisory"
    assert (validation.rollup.accepted_gating, validation.rollup.accepted_advisory) == (0, 1)
    assert PINNED_VALIDATION.read_bytes() == raw
