"""Per-control validation verdicts (issue #146).

Covers the three verdict paths plus skip honesty. The verdict logic is pure so
the rejection paths are exercised directly; the accepted path runs the real
replay end to end against the control-demo bundle, because the thing worth
proving is that a real control earns its verdict from real re-runs.
"""

from __future__ import annotations

import json

from conftest import FIXTURES_DIR
from trace_harness.cli import main
from trace_harness.environment.controls import REFUND_WINDOW_CONTROL_ID
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


def _bundle_artifact(tmp_path):
    runs_dir = tmp_path / "runs_bundle"
    assert main(["--runs-dir", str(runs_dir), "run-pipeline", str(CONTROL_DEMO_TASK_PATH)]) == 0
    (run_dir,) = [p for p in runs_dir.iterdir() if p.is_dir()]
    return run_dir / names.REGRESSION_ARTIFACT


def _validation_for(tmp_path, extra_args=()):
    """Replay the demo bundle with controls and return the written artifact."""
    artifact = _bundle_artifact(tmp_path)
    replay_dir = tmp_path / "runs_replay"
    code = main(
        ["--runs-dir", str(replay_dir), "replay", str(artifact), "--apply-control", *extra_args]
    )
    source_run_id = json.loads(artifact.read_text())["source_run_id"]
    data = json.loads(
        (replay_dir / source_run_id / names.REPAIR_VALIDATION).read_text(encoding="utf-8")
    )
    return code, RepairValidation.model_validate(data)


# --- the accepted path, end to end ---


def test_refund_guardrail_is_accepted_with_rerun_evidence(tmp_path) -> None:
    code, validation = _validation_for(tmp_path)

    assert code == 0
    assert validation.schema_version == REPAIR_VALIDATION_SCHEMA_VERSION
    accepted = [c for c in validation.controls if c.verdict is ControlVerdict.ACCEPTED]
    assert [c.control for c in accepted] == ["deterministic_pre_call_refund_guardrail"]

    (control,) = accepted
    assert control.control_id == REFUND_WINDOW_CONTROL_ID
    assert control.guardrail_ref == "unauthorized_cash_refund_guardrail"
    assert control.originating_rerun is not None
    assert control.originating_rerun.cleared_checks  # the pinned checks stopped firing
    assert control.originating_rerun.run_id  # linked to the re-run that proved it


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
