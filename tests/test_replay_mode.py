"""TRA-94: static replay trust is earned by all four rules, never by a PASS alone."""

import json

import pytest

from conftest import FIXTURES_DIR
from trace_harness.cli import main
from trace_harness.regression.materializer import classify_replay_mode
from trace_harness.regression.schemas import RegressionArtifact, ReplayModeBasis
from trace_harness.runner.batch import BatchRunner
from trace_harness.runner.suite import load_suite
from trace_harness.tracing.artifact_store import ArtifactStore


def static_basis():
    return ReplayModeBasis(
        control_ids=["synthetic_complete_control"],
        control_step=2,
        first_irreversible_action_step=2,
        steps_remaining_after_control=1,
        gated_tool="issue_refund",
        checks_reachable_via_gated_tool=["unauthorized_cash_refund", "unauthorized_store_credit"],
        checks_covered_by_control=["unauthorized_cash_refund", "unauthorized_store_credit"],
        rule_kind="prohibition",
    )


def test_all_four_rules_allow_static_replay():
    assert classify_replay_mode(static_basis()) == "static_ok"


@pytest.mark.parametrize(
    "change",
    [
        {"control_step": 3},  # Harm has already happened at step 2.
        {"rule_kind": "requirement"},
        {"checks_covered_by_control": ["unauthorized_cash_refund"]},
        {"other_irreversible_tools": ["transfer_money"]},
        {"control_step": None, "first_irreversible_action_step": None},
        {"checks_reachable_via_gated_tool": []},
        {"control_ids": []},
        {"rule_kind": None},
    ],
)
def test_any_failed_or_unknown_rule_requires_live(change):
    assert classify_replay_mode(static_basis().model_copy(update=change)) == "live_required"


def test_five_bundle_labels_are_pinned_and_none_is_static_ok(tmp_path):
    store = ArtifactStore(tmp_path)
    summary = BatchRunner(store).run(load_suite(FIXTURES_DIR / "suites/refund_bundles_v0.json"))
    actual = {}
    for entry in summary.entries:
        if entry.verifier_passed is not False:
            continue
        artifact = RegressionArtifact.model_validate_json(
            (store.run_dir(entry.run_id) / "regression_artifact.json").read_text()
        )
        actual[entry.task_id] = artifact.replay_mode
        assert artifact.replay_mode_basis is not None
        assert artifact.replay_mode_basis.predicted_by == "heuristic_v1"
        assert artifact.replay_mode_basis.agreement_rate is None
    expected = json.loads(
        (FIXTURES_DIR / "expected/refund_bundles_v0_replay_modes.json").read_text()
    )
    assert actual == expected
    assert len(actual) == 5
    assert set(actual.values()) == {"live_required"}, "Cash-only guardrail cannot earn static_ok"


def test_demo_label_basis_warning_and_legacy_loading(tmp_path, capsys):
    runs = tmp_path / "source"
    assert (
        main(
            [
                "--runs-dir",
                str(runs),
                "run-pipeline",
                str(FIXTURES_DIR / "tasks/refund_policy_control_demo.json"),
            ]
        )
        == 0
    )
    path = next(runs.glob("*/regression_artifact.json"))
    data = json.loads(path.read_text())
    assert data["schema_version"] == "0.3.0"
    assert data["replay_mode"] == "live_required"
    assert data["replay_mode_basis"] == {
        "control_ids": ["ctl_refund_window_v1"],
        "control_step": 2,
        "first_irreversible_action_step": 2,
        "steps_remaining_after_control": 1,
        "gated_tool": "issue_refund",
        "checks_reachable_via_gated_tool": [
            "unauthorized_cash_refund",
            "unauthorized_store_credit",
        ],
        "checks_covered_by_control": ["unauthorized_cash_refund"],
        "other_irreversible_tools": [],
        "rule_kind": "prohibition",
        "predicted_by": "heuristic_v1",
        "agreement_rate": None,
        "source_experiment_id": None,
    }
    capsys.readouterr()
    for flags in ([], ["--apply-control"]):
        assert main(["--runs-dir", str(tmp_path / "replays"), "replay", str(path), *flags]) == 0
        output = capsys.readouterr().out
        assert "replay_mode:" in output and "live_required" in output
        assert ("a live agent must continue from the block point" in output) == bool(flags)
    data["schema_version"] = "0.2.0"
    del data["replay_mode"], data["replay_mode_basis"]
    legacy = RegressionArtifact.model_validate(data)
    assert legacy.replay_mode == "unlabeled"
    assert legacy.replay_mode_basis is None
    path.write_text(json.dumps(data))
    assert main(["--runs-dir", str(tmp_path / "legacy"), "replay", str(path)]) == 0
    assert "unlabeled" in capsys.readouterr().out


def test_materializer_uses_executable_coverage_not_failed_check_list(tmp_path, monkeypatch):
    """A synthetic complete guardrail qualifies; changing its rule kind disqualifies it."""
    from dataclasses import replace

    from trace_harness.environment.controls import GUARDRAIL_REGISTRY

    ref = "unauthorized_cash_refund_guardrail"
    original = GUARDRAIL_REGISTRY[ref]
    for rule_kind, expected in (("prohibition", "static_ok"), ("requirement", "live_required")):
        monkeypatch.setitem(
            GUARDRAIL_REGISTRY,
            ref,
            replace(
                original,
                checks_covered=frozenset({"unauthorized_cash_refund", "unauthorized_store_credit"}),
                rule_kind=rule_kind,
            ),
        )
        runs = tmp_path / rule_kind
        assert (
            main(
                [
                    "--runs-dir",
                    str(runs),
                    "run-pipeline",
                    str(FIXTURES_DIR / "tasks/refund_policy_control_demo.json"),
                ]
            )
            == 0
        )
        data = json.loads(next(runs.glob("*/regression_artifact.json")).read_text())
        assert data["replay_mode"] == expected
        assert data["replay_mode_basis"]["control_step"] == 2
