"""validate-control end to end on #200's own derivation (#203).

It uses no stand-in. experiment record must derive verdict_agreement_rate and
sibling_failure_rate itself and write repair_effectiveness.json beside
result.json on the path validate-control shares with it. Until #200 is merged
the module it adds is missing and the test is skipped, since both metrics are
null there, no sidecar exists and the decision is review, which
test_validate_control.py pins.

The conditions are the offline ones from tests/test_validate_control.py. The
control demo with the valid-cash sibling forks at step 1. The live arm's
script tries the recorded cash refund, which the refund window control blocks
on all five seeds, and answers in its own words, and the noise floor's script
lets the refund through on all five.
"""

from __future__ import annotations

import pytest

from conftest import REPO_ROOT
from test_validate_control import (
    EXPERIMENT_ID,
    HAS_200,
    LIVE_CHECKS,
    RULE,
    _arms,
    _checks,
    _demo_artifact,
    _plan,
    _result,
    _validate,
)
from trace_harness.environment.controls import REFUND_WINDOW_CONTROL_ID
from trace_harness.runner.repair_effectiveness import (
    REPAIR_EFFECTIVENESS_FILE,
    RepairEffectivenessReport,
)
from trace_harness.tracing.artifact_store import ArtifactStore

pytestmark = pytest.mark.skipif(not HAS_200, reason="needs #200's metrics and sidecar")


@pytest.fixture(autouse=True)
def _from_the_repository_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(REPO_ROOT)


def test_the_refund_window_control_keeps_on_the_metrics_record_derives(tmp_path):
    path, artifact = _demo_artifact(tmp_path)
    plan = _plan(tmp_path, *_arms(tmp_path, artifact))

    assert _validate(tmp_path, plan, path, REFUND_WINDOW_CONTROL_ID) == 0

    result = _result(tmp_path)
    # #200's derivation, read back from the result record wrote.
    assert result.metrics.verdict_agreement_rate == 1.0
    assert result.metrics.sibling_failure_rate == 0.0
    assert result.metrics.post_block_outcomes == {"recovered": 5}

    # #200's sidecar, beside the result, with the one entry the rule reads.
    sidecar = ArtifactStore(tmp_path / "runs").experiment_dir(EXPERIMENT_ID)
    report = RepairEffectivenessReport.model_validate_json(
        (sidecar / REPAIR_EFFECTIVENESS_FILE).read_text()
    )
    (entry,) = [
        e
        for e in report.entries
        if e.control_id == REFUND_WINDOW_CONTROL_ID
        and e.control_on.condition == "live"
        and e.control_off.condition == "live_no_control"
    ]
    assert entry.artifact_id in {artifact["test_name"], artifact["source_run_id"]}
    assert (entry.control_on.batch_id, entry.control_off.batch_id) == (
        result.condition_batches["live"],
        result.condition_batches["live_no_control"],
    )
    assert (entry.control_on.blocking_failures_after_fork, entry.control_on.completed_runs) == (
        0,
        5,
    )
    assert (entry.control_off.blocking_failures_after_fork, entry.control_off.completed_runs) == (
        5,
        5,
    )
    assert entry.repair_effectiveness == 1.0

    assert (result.decision.value, result.decided_by.value) == ("keep", "policy")
    assert _checks(result) == LIVE_CHECKS
    assert result.metadata["validate_control"]["keep_rule"] == RULE.model_dump()
