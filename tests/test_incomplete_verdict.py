"""Issue #163: a run that never finished is `incomplete`, never `pass`.

Reproduces the bug end to end (truncated script -> terminated run) and checks
every consumer: verifier_result.json, the run index, list-runs, the batch
aggregates, and the bundle stage (nothing is bundled for an incomplete run).
"""

from __future__ import annotations

import json

import pytest

from conftest import FIXTURES_DIR
from trace_harness.cli import main
from trace_harness.runner.batch import BatchSummary
from trace_harness.runner.suite import AgentConfig
from trace_harness.tracing import artifact_store as names
from trace_harness.tracing.artifact_store import ArtifactStore
from trace_harness.verifiers.base import VerifierResult, VerifierVerdict, mark_incomplete

DEMO_TASK = FIXTURES_DIR / "tasks" / "refund_policy_control_demo.json"
DEMO_SCRIPT = FIXTURES_DIR / "scripts" / "refund_policy_control_demo_script.json"


def _truncated_script(tmp_path):
    script = json.loads(DEMO_SCRIPT.read_text())
    script["actions"] = script["actions"][:1]  # get_order only; never reaches a final answer
    script["script_id"] += "_truncated"
    path = tmp_path / "short_script.json"
    path.write_text(json.dumps(script))
    return path


def _only_run_id(runs_dir):
    (run_dir,) = [p for p in runs_dir.iterdir() if p.is_dir() and p.name.startswith("run_")]
    return run_dir.name


def test_truncated_run_is_incomplete_everywhere(tmp_path, capsys):
    runs_dir = tmp_path / "runs"
    code = main(
        [
            "--runs-dir",
            str(runs_dir),
            "run-pipeline",
            str(DEMO_TASK),
            "--script",
            str(_truncated_script(tmp_path)),
        ]
    )
    assert code == 0  # not --fail-on-verifier; the pipeline itself succeeds
    out = capsys.readouterr().out
    assert "Verifier verdict for" in out and "INCOMPLETE" in out
    run_id = _only_run_id(runs_dir)
    store = ArtifactStore(runs_dir)

    verifier = VerifierResult.model_validate(store.read_json(run_id, names.VERIFIER_RESULT))
    assert verifier.verdict is VerifierVerdict.INCOMPLETE
    assert verifier.passed is False
    assert verifier.failed_checks == []  # no violation happened before it died
    assert any("did not complete" in w for w in verifier.warnings)

    entry = next(e for e in store.read_index().entries if e.run_id == run_id)
    assert entry.status == "terminated"
    assert entry.verdict == "incomplete"
    assert entry.verifier_passed is False

    # nothing to attribute or bundle: no verified failure exists
    for artifact in (names.ATTRIBUTION_RESULT, names.FAILURE_CARD, names.REGRESSION_ARTIFACT):
        assert not store.exists(run_id, artifact)

    main(["--runs-dir", str(runs_dir), "list-runs"])
    listed = capsys.readouterr().out
    assert run_id in listed and "INCOMPLETE" in listed and "PASS" not in listed


def test_fail_on_verifier_still_fails_incomplete(tmp_path):
    runs_dir = tmp_path / "runs"
    code = main(
        [
            "--runs-dir",
            str(runs_dir),
            "run-pipeline",
            str(DEMO_TASK),
            "--script",
            str(_truncated_script(tmp_path)),
            "--fail-on-verifier",
        ]
    )
    assert code == 1


def test_attribute_and_bundle_refuse_incomplete_runs(tmp_path, capsys):
    runs_dir = tmp_path / "runs"
    main(
        [
            "--runs-dir",
            str(runs_dir),
            "run-fixture",
            str(DEMO_TASK),
            "--script",
            str(_truncated_script(tmp_path)),
        ]
    )
    run_id = _only_run_id(runs_dir)
    main(["--runs-dir", str(runs_dir), "verify", str(runs_dir / run_id)])
    main(["--runs-dir", str(runs_dir), "attribute", str(runs_dir / run_id)])
    assert "is incomplete; nothing to attribute" in capsys.readouterr().out
    store = ArtifactStore(runs_dir)
    assert not store.exists(run_id, names.ATTRIBUTION_RESULT)


def test_batch_aggregates_count_incomplete_and_exclude_from_pass_rate(tmp_path):
    # max_steps=1 stops the scripted agent before its final answer -> terminated -> incomplete
    suite = {
        "schema_version": "0.1.0",
        "suite_id": "incomplete_probe",
        "tasks": [
            str(FIXTURES_DIR / "tasks" / "refund_policy_valid_cash.json"),
            str(DEMO_TASK),
        ],
        "agent_configs": [
            AgentConfig(label="full").model_dump(mode="json"),
            AgentConfig(label="cut_short", max_steps=1).model_dump(mode="json"),
        ],
    }
    suite_path = tmp_path / "suite.json"
    suite_path.write_text(json.dumps(suite))
    runs_dir = tmp_path / "runs"
    assert main(["--runs-dir", str(runs_dir), "run-suite", str(suite_path)]) == 0
    store = ArtifactStore(runs_dir)
    (batch_id,) = [p.name for p in (runs_dir / names.BATCHES_DIR).iterdir()]
    summary = BatchSummary.model_validate(
        json.loads(store.batch_summary_path(batch_id).read_text())
    )

    agg = summary.aggregates
    assert agg.total == 4
    assert agg.incomplete == 2
    assert agg.terminated == 2
    assert (
        agg.verifier_passed + agg.verifier_failed == 2
    )  # only the two completed runs have verdicts
    assert agg.pass_rate == pytest.approx(agg.verifier_passed / 2)
    assert agg.by_agent["cut_short"]["incomplete"] == 2
    assert {e.verdict for e in summary.entries if e.agent_label == "cut_short"} == {"incomplete"}
    assert all(e.verifier_passed is False for e in summary.entries if e.verdict == "incomplete")


def test_pre_0_4_0_verifier_file_derives_verdict_from_passed():
    old = {"schema_version": "0.3.0", "verifier_id": "x", "run_id": "r", "passed": True}
    assert VerifierResult.model_validate(old).verdict is VerifierVerdict.PASS
    old["passed"] = False
    assert VerifierResult.model_validate(old).verdict is VerifierVerdict.FAIL


def test_mark_incomplete_keeps_checks_and_forces_passed_false():
    from trace_harness.tasks.schemas import Severity
    from trace_harness.verifiers.base import FailedCheck

    check = FailedCheck(check_id="c", message="m", expected="e", actual="a", severity=Severity.HIGH)
    result = VerifierResult(verifier_id="v", run_id="r", passed=False, failed_checks=[check])
    marked = mark_incomplete(result, status="error", termination_reason="model_error")
    assert marked.verdict is VerifierVerdict.INCOMPLETE
    assert marked.passed is False
    assert marked.failed_checks == [check]
    assert marked.blocks_release == result.blocks_release
    with pytest.raises(ValueError):
        VerifierResult(verifier_id="v", run_id="r", passed=True, verdict=VerifierVerdict.INCOMPLETE)
