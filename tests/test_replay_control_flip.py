"""Integration tests for `trace-harness replay --apply-control`.

These prove the two claims made in docs/regression_contract.md:
    1. On the minimal single-violation fixture, blocking the unauthorized
       cash refund is *sufficient by itself* to flip the whole run from FAIL
       to PASS — no agent adaptation required.
    2. On the full staged-failure fixture, the same guardrail only clears
       the one check it actually targets; checks tied to the scripted
       agent's fixed narration (ticket notes, final answer) still fire,
       because a guardrail can only change what happens in state.
The second case also confirms the guardrail does not overblock the positive
sibling (a refund that should be allowed either way).
"""

from __future__ import annotations

import json

from conftest import FIXTURES_DIR
from trace_harness.cli import main
from trace_harness.tracing import artifact_store as names

CONTROL_DEMO_TASK_PATH = FIXTURES_DIR / "tasks" / "refund_policy_control_demo.json"
FAILURE_TASK_PATH = FIXTURES_DIR / "tasks" / "refund_policy_failure.json"


def _only_run_dir(runs_dir):
    run_dirs = [p for p in runs_dir.iterdir() if p.is_dir()]
    assert len(run_dirs) == 1
    return run_dirs[0]


def _bundle_and_get_artifact_path(tmp_path, task_path, name):
    runs_dir = tmp_path / f"runs_{name}"
    assert main(["--runs-dir", str(runs_dir), "run-pipeline", str(task_path)]) == 0
    run_dir = _only_run_dir(runs_dir)
    artifact_path = run_dir / names.REGRESSION_ARTIFACT
    assert artifact_path.is_file()
    return artifact_path


def test_minimal_fixture_reproduces_without_control(tmp_path):
    """No guardrail: this is a plain regression check — the bug must still reproduce."""
    artifact_path = _bundle_and_get_artifact_path(tmp_path, CONTROL_DEMO_TASK_PATH, "a")
    exit_code = main(["--runs-dir", str(tmp_path / "runs_replay_a"), "replay", str(artifact_path)])
    assert exit_code == 0


def test_minimal_fixture_flips_clean_with_control(tmp_path, capsys):
    """The whole point of this fixture: one guardrail is enough for a clean FAIL -> PASS."""
    artifact_path = _bundle_and_get_artifact_path(tmp_path, CONTROL_DEMO_TASK_PATH, "b")
    runs_dir = tmp_path / "runs_replay_b"
    exit_code = main(["--runs-dir", str(runs_dir), "replay", str(artifact_path), "--apply-control"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "control eliminated" in out
    assert "PASS — regression gate clear" in out


def test_full_fixture_only_partially_flips_with_control(tmp_path, capsys):
    """The 4-check fixture must NOT fully clear — escalation/narration checks persist."""
    artifact_path = _bundle_and_get_artifact_path(tmp_path, FAILURE_TASK_PATH, "c")
    artifact = json.loads(artifact_path.read_text())
    assert artifact["verifier_checks"] == [
        "required_escalation_missing",
        "unauthorized_cash_refund",
        "ticket_outage_claim_unsupported",
        "deprecated_policy_treated_as_authoritative",
    ]
    capsys.readouterr()  # discard the uncontrolled bundling run's own printout above

    runs_dir = tmp_path / "runs_replay_c"
    exit_code = main(["--runs-dir", str(runs_dir), "replay", str(artifact_path), "--apply-control"])
    out = capsys.readouterr().out

    assert exit_code == 1  # gate still fires — the control did not clear every pinned check
    assert "still fired" in out
    # The guardrail's own target is gone; the checks tied to fixed narration remain.
    assert "unauthorized_cash_refund" not in out
    assert "required_escalation_missing" in out
    assert "ticket_outage_claim_unsupported" in out
    assert "deprecated_policy_treated_as_authoritative" in out
    # The guardrail doesn't just fail to clear the others — it introduces a NEW
    # failure: the hardcoded final answer ("I've issued a full cash refund...")
    # was true before the block and is false once the refund is blocked.
    assert "final_answer_inconsistent_with_state" in out
    # The positive sibling must still pass with the guardrail active — no overblocking.
    assert "[1] valid_cash_refund_within_window" in out
    assert out.count("PASS") >= 1
