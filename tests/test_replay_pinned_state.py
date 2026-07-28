"""Tests for replaying a regression artifact from its *pinned* world.

The point of pinning ``initial_state``/``pinned_docs`` is that a regression
keeps testing the scenario the failure actually happened in. So the load-
bearing behavior is: when the artifact and the live fixture disagree, the
artifact wins, and the disagreement gets reported rather than silently
absorbed.

These tests express that by editing the *artifact* and leaving the repo
fixture alone — the reverse of how it happens in real life (fixture edited,
artifact untouched), but it exercises the same divergence without mutating
files other tests depend on.
"""

from __future__ import annotations

import json
from pathlib import Path

from conftest import FIXTURES_DIR
from trace_harness.cli import main
from trace_harness.regression.replay import describe_action_drift, describe_state_drift
from trace_harness.regression.schemas import REGRESSION_SCHEMA_VERSION
from trace_harness.tracing import artifact_store as names

CONTROL_DEMO_TASK_PATH = FIXTURES_DIR / "tasks" / "refund_policy_control_demo.json"
FAILURE_TASK_PATH = FIXTURES_DIR / "tasks" / "refund_policy_failure.json"


def _bundle_artifact(tmp_path: Path, task_path: Path, name: str) -> Path:
    runs_dir = tmp_path / f"runs_{name}"
    assert main(["--runs-dir", str(runs_dir), "run-pipeline", str(task_path)]) == 0
    run_dirs = [p for p in runs_dir.iterdir() if p.is_dir()]
    assert len(run_dirs) == 1
    artifact_path = run_dirs[0] / names.REGRESSION_ARTIFACT
    assert artifact_path.is_file()
    return artifact_path


def _rewrite(artifact_path: Path, dest: Path, **changes) -> Path:
    artifact = json.loads(artifact_path.read_text())
    artifact.update(changes)
    dest.write_text(json.dumps(artifact, indent=2))
    return dest


# --- replay uses the pinned world, not the fixture's current one ---


def test_pinned_state_drives_the_run_not_the_fixture(tmp_path, capsys):
    """Age the pinned order back into policy: the pinned value must decide the verdict.

    The fixture on disk still says 47 days (out of policy), so if replay read
    the fixture the failure would reproduce and the gate would pass. Reading
    the artifact instead means the refund is now allowed, the pinned check
    can't reproduce, and the gate correctly fires.
    """
    artifact_path = _bundle_artifact(tmp_path, CONTROL_DEMO_TASK_PATH, "a")
    artifact = json.loads(artifact_path.read_text())
    artifact["initial_state"]["orders"][0]["purchase_age_days"] = 5
    tampered = tmp_path / "in_policy.json"
    tampered.write_text(json.dumps(artifact, indent=2))
    capsys.readouterr()

    exit_code = main(["--runs-dir", str(tmp_path / "replay_a"), "replay", str(tampered)])
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "expected verifier to fail" in out


def test_fixture_drift_is_reported(tmp_path, capsys):
    artifact_path = _bundle_artifact(tmp_path, CONTROL_DEMO_TASK_PATH, "b")
    artifact = json.loads(artifact_path.read_text())
    artifact["initial_state"]["orders"][0]["purchase_age_days"] = 5
    tampered = tmp_path / "drifted.json"
    tampered.write_text(json.dumps(artifact, indent=2))
    capsys.readouterr()

    main(["--runs-dir", str(tmp_path / "replay_b"), "replay", str(tampered)])
    out = capsys.readouterr().out

    assert "fixture drift" in out
    assert "purchase_age_days: 5 -> 47" in out


def test_unmodified_artifact_reports_no_drift(tmp_path, capsys):
    """The docs-heavy fixture round-trips cleanly: pinned snapshot == fixture world."""
    artifact_path = _bundle_artifact(tmp_path, FAILURE_TASK_PATH, "c")
    capsys.readouterr()

    main(["--runs-dir", str(tmp_path / "replay_c"), "replay", str(artifact_path)])
    out = capsys.readouterr().out

    assert "fixture drift" not in out


def test_pinned_docs_used_when_initial_state_has_no_docs(tmp_path, capsys):
    """Older artifacts pinned docs only alongside state; replay must still see them.

    ``deprecated_policy_treated_as_authoritative`` can only fire if the doc
    corpus is present, so its reproduction proves the fallback wired the docs
    into the replayed world.
    """
    artifact_path = _bundle_artifact(tmp_path, FAILURE_TASK_PATH, "d")
    artifact = json.loads(artifact_path.read_text())
    assert artifact["pinned_docs"], "fixture should pin a doc corpus"
    artifact["initial_state"]["docs"] = []
    docless = tmp_path / "docless_state.json"
    docless.write_text(json.dumps(artifact, indent=2))
    capsys.readouterr()

    exit_code = main(["--runs-dir", str(tmp_path / "replay_d"), "replay", str(docless)])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "all expected checks failed as expected" in out


def test_replay_run_records_that_it_used_pinned_state(tmp_path):
    """The run's own config should say so, so a stored run is self-describing."""
    artifact_path = _bundle_artifact(tmp_path, CONTROL_DEMO_TASK_PATH, "e")
    runs_dir = tmp_path / "replay_e"
    assert main(["--runs-dir", str(runs_dir), "replay", str(artifact_path)]) == 0

    run_dir = next(p for p in runs_dir.iterdir() if p.is_dir())
    config = json.loads((run_dir / names.RUN_CONFIG).read_text())
    assert config["metadata"]["replay_pinned_state"] == "true"


# --- the agent's actions are pinned too, not just the world ---


def test_pinned_actions_drive_the_run_not_the_script(tmp_path, capsys):
    """Drop the violating call from the pinned actions; the script still has it.

    If replay read the script file, the refund would still be issued and the
    pinned check would reproduce. Running the pinned actions instead means no
    refund happens, so the check can't reproduce and the gate fires.
    """
    artifact_path = _bundle_artifact(tmp_path, CONTROL_DEMO_TASK_PATH, "g")
    artifact = json.loads(artifact_path.read_text())
    artifact["pinned_agent_actions"] = [
        action
        for action in artifact["pinned_agent_actions"]
        if (action.get("tool_call") or {}).get("tool_name") != "issue_refund"
    ]
    edited = tmp_path / "no_refund_actions.json"
    edited.write_text(json.dumps(artifact, indent=2))
    capsys.readouterr()

    exit_code = main(["--runs-dir", str(tmp_path / "replay_g"), "replay", str(edited)])
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "expected verifier to fail" in out
    assert "script length: 2 pinned action(s) -> 3 in the fixture" in out


def test_artifact_pins_actions_and_schema_versions(tmp_path):
    artifact_path = _bundle_artifact(tmp_path, CONTROL_DEMO_TASK_PATH, "h")
    artifact = json.loads(artifact_path.read_text())

    assert artifact["schema_version"] == REGRESSION_SCHEMA_VERSION
    assert [a["kind"] for a in artifact["pinned_agent_actions"]] == [
        "tool_call",
        "tool_call",
        "final_answer",
    ]
    # Every input shape the artifact depends on is recorded, so a future reader
    # can tell a real regression from a schema that moved.
    versions = artifact["metadata"]["schema_versions"]
    assert set(versions) == {"task", "state", "trace", "verifier_result", "harness"}
    assert all(versions.values()), f"no version may be missing: {versions}"
    assert artifact["metadata"]["available_tools"] == ["get_order", "issue_refund"]


def test_pre_0_2_0_artifact_without_pinned_actions_falls_back_to_the_script(tmp_path, capsys):
    """Older artifacts pinned no actions; they must still replay from the file."""
    artifact_path = _bundle_artifact(tmp_path, CONTROL_DEMO_TASK_PATH, "i")
    legacy = _rewrite(artifact_path, tmp_path / "legacy.json", pinned_agent_actions=[])
    capsys.readouterr()

    exit_code = main(["--runs-dir", str(tmp_path / "replay_i"), "replay", str(legacy)])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "(no actions)" in out
    assert "all expected checks failed as expected" in out


# --- a control that relocates a failure is not a clear gate ---


def test_control_that_moves_a_failure_fires_the_gate(tmp_path, capsys):
    """Pinned check cleared, but a new blocking check appeared: must still fail.

    Narrowing ``verifier_checks`` to the one check the guardrail covers is the
    shape of a real single-violation artifact. On this fixture the scripted
    agent still claims the blocked refund happened, so clearing the refund
    check surfaces ``final_answer_inconsistent_with_state`` — the failure
    moved rather than went away, and the gate must say so.
    """
    artifact_path = _bundle_artifact(tmp_path, FAILURE_TASK_PATH, "f")
    narrowed = _rewrite(
        artifact_path,
        tmp_path / "narrowed.json",
        verifier_checks=["unauthorized_cash_refund"],
    )
    capsys.readouterr()

    exit_code = main(
        ["--runs-dir", str(tmp_path / "replay_f"), "replay", str(narrowed), "--apply-control"]
    )
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "control eliminated ['unauthorized_cash_refund']" in out
    assert "never pinned" in out
    assert "final_answer_inconsistent_with_state" in out


# --- describe_state_drift unit coverage ---


def test_drift_empty_for_identical_states():
    state = {"orders": [{"order_id": "O-1", "purchase_age_days": 47}], "docs": []}
    assert describe_state_drift(state, json.loads(json.dumps(state))) == []


def test_drift_detects_changed_field():
    pinned = {"orders": [{"order_id": "O-1", "purchase_age_days": 47}]}
    live = {"orders": [{"order_id": "O-1", "purchase_age_days": 12}]}
    notes = describe_state_drift(pinned, live)
    assert notes == ["orders[O-1] changed (purchase_age_days: 47 -> 12)"]


def test_drift_detects_added_and_removed_records():
    pinned = {"docs": [{"doc_id": "policy_v4"}, {"doc_id": "policy_v2"}]}
    live = {"docs": [{"doc_id": "policy_v4"}, {"doc_id": "policy_v5"}]}
    notes = describe_state_drift(pinned, live)
    assert "docs: pinned ['policy_v2'] no longer in the fixture" in notes
    assert "docs: fixture has new ['policy_v5'] not in the pinned run" in notes


def test_drift_detects_scalar_keys_including_schema_version():
    notes = describe_state_drift(
        {"schema_version": "0.1.0", "doc_ranking_override": None},
        {"schema_version": "0.2.0", "doc_ranking_override": ["a"]},
    )
    assert "schema_version: '0.1.0' -> '0.2.0'" in notes
    assert "doc_ranking_override: None -> ['a']" in notes


# --- describe_action_drift unit coverage ---


def _tool_action(tool_name: str, reasoning: str = "because") -> dict:
    return {
        "kind": "tool_call",
        "tool_call": {"tool_name": tool_name, "arguments": {}},
        "final_answer": None,
        "reasoning": reasoning,
    }


def test_action_drift_empty_for_identical_scripts():
    actions = [_tool_action("get_order")]
    assert describe_action_drift(actions, json.loads(json.dumps(actions))) == []


def test_action_drift_ignores_reasoning_changes():
    """Reasoning is authoring commentary; rewording it is not behavior drift."""
    pinned = [_tool_action("get_order", reasoning="original wording")]
    live = [_tool_action("get_order", reasoning="completely rewritten wording")]
    assert describe_action_drift(pinned, live) == []


def test_action_drift_reports_a_changed_tool_call():
    notes = describe_action_drift([_tool_action("get_order")], [_tool_action("issue_refund")])
    assert len(notes) == 1
    assert "action 1 changed" in notes[0]
    assert "issue_refund" in notes[0]


def test_action_drift_reports_length_change():
    notes = describe_action_drift([_tool_action("get_order")], [])
    assert "script length: 1 pinned action(s) -> 0 in the fixture" in notes


def test_action_drift_truncates_long_values():
    """A paragraph-length final answer must not produce an unreadable line."""
    pinned = [{"kind": "final_answer", "final_answer": "x" * 500, "tool_call": None}]
    live = [{"kind": "final_answer", "final_answer": "y" * 500, "tool_call": None}]
    notes = describe_action_drift(pinned, live)
    assert len(notes) == 1
    assert "..." in notes[0]
    assert len(notes[0]) < 250
