"""The three trend measures and the history file (#207).

Every number here is checked against a fixture tree built in the test, so a
change to a formula shows up as a changed expectation rather than as a quiet
shift in a plotted line.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import REPO_ROOT
from trace_harness.metrics.history import (
    MetricsSnapshot,
    append_snapshot,
    build_snapshot,
    compute_cost_of_learning,
    compute_coverage,
    compute_over_blocking,
    find_repair_validations,
    latest_validation,
    load_history,
    prescribed_controls,
)
from trace_harness.regression.repair_validation import RepairValidation

# A prescribed control with a registered guardrail, so coverage has something real
# to count as materializable.
MATERIAL = "deterministic_pre_call_refund_guardrail"
# A prescribed control that still has no guardrail after #194, so coverage has
# something real to count as unmaterializable.
PAPER = "escalation_discipline_check"


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _rerun(run_id: str, verdict: str, task_id: str = "t") -> dict:
    return {"run_id": run_id, "task_id": task_id, "verdict": verdict}


def _run_dir(root: Path, run_id: str, *, refunds: list[float], irreversible: int) -> None:
    """A minimal retained run: a trace with tool calls and a final state."""
    directory = root / "validation" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    events = []
    for index in range(irreversible):
        events.append(
            {
                "event_type": "tool_call_executed",
                "step_id": index,
                "payload": {
                    "tool_name": "issue_refund",
                    "status": "ok",
                    "side_effect": "external_irreversible",
                },
            }
        )
    # A failed irreversible call and a reversible one, neither of which counts.
    events.append(
        {
            "event_type": "tool_call_executed",
            "payload": {"status": "error", "side_effect": "external_irreversible"},
        }
    )
    events.append(
        {
            "event_type": "tool_call_executed",
            "payload": {"status": "ok", "side_effect": "read_only"},
        }
    )
    (directory / "trace.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
    )
    _write(
        directory / "final_state.json",
        {"refunds": [{"refund_id": f"R{i}", "amount_usd": a} for i, a in enumerate(refunds)]},
    )


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """Two prescribed controls, one accepted, one validation artifact, two re-runs."""
    _write(
        tmp_path / "acceptance" / "run_a" / "repair_package.json",
        {"controls": [{"name": MATERIAL}, {"name": PAPER}]},
    )
    _write(
        tmp_path / "evidence" / "repair_validation.json",
        {
            "run_id": "run_a",
            "test_name": "regression_a",
            "batch_id": "batch_20260101T000000Z_aaaa",
            "controls": [
                {
                    "control": MATERIAL,
                    "verdict": "accepted",
                    "originating_rerun": _rerun("rr_origin", "PASS"),
                    "sibling_reruns": [
                        _rerun("rr_sib_ok", "PASS"),
                        _rerun("rr_sib_bad", "FAIL"),
                    ],
                },
                {"control": PAPER, "verdict": "skipped", "reason": "no guardrail"},
            ],
        },
    )
    _run_dir(tmp_path / "evidence", "rr_origin", refunds=[100.0], irreversible=1)
    _run_dir(tmp_path / "evidence", "rr_sib_ok", refunds=[50.5, 9.5], irreversible=2)
    _run_dir(tmp_path / "evidence", "rr_sib_bad", refunds=[], irreversible=0)
    _write(
        tmp_path / "acceptance" / "refund_batch_summary.json",
        {"aggregates": {"verifier_passed": 7, "verifier_failed": 3}},
    )
    return tmp_path


def test_prescribed_controls_are_the_names_packages_asked_for(tree: Path) -> None:
    assert prescribed_controls(tree) == {MATERIAL, PAPER}


def test_coverage_counts_each_wall_separately(tree: Path) -> None:
    validations = [v for _, v in find_repair_validations(tree)]
    coverage = compute_coverage(prescribed_controls(tree), validations)
    assert coverage.prescribed == 2
    assert coverage.materializable == 1  # only MATERIAL has a guardrail
    assert coverage.validated == 2  # both got a verdict, one of them "skipped"
    assert coverage.accepted == 1
    assert coverage.accepted_over_prescribed.value == 0.5
    assert coverage.unmapped_controls == []


def test_coverage_reports_null_rather_than_zero_when_nothing_was_prescribed(
    tmp_path: Path,
) -> None:
    """0/0 drawn as 0.0 reads as total failure. It means nothing was measured."""
    coverage = compute_coverage(set(), [])
    assert coverage.accepted_over_prescribed.value is None


def test_coverage_names_a_prescribed_control_absent_from_the_map(tmp_path: Path) -> None:
    coverage = compute_coverage({"invented_control"}, [])
    assert coverage.unmapped_controls == ["invented_control"]


def test_over_blocking_is_the_sibling_failure_rate(tree: Path) -> None:
    pairs = find_repair_validations(tree)
    over = compute_over_blocking(latest_validation(pairs), root=tree)
    assert (over.siblings_failed, over.siblings_run) == (1, 2)
    assert over.rate.value == 0.5
    assert over.sources == ["evidence/repair_validation.json"]


def test_over_blocking_reads_the_latest_artifact_not_all_of_them(tree: Path) -> None:
    """A control rejected months ago must not drag today's number down."""
    _write(
        tree / "later" / "repair_validation.json",
        {
            "run_id": "run_a",
            "test_name": "regression_a",
            "batch_id": "batch_20270101T000000Z_zzzz",
            "controls": [
                {
                    "control": MATERIAL,
                    "verdict": "accepted",
                    "sibling_reruns": [_rerun("rr_new", "PASS")],
                }
            ],
        },
    )
    pairs = find_repair_validations(tree)
    assert len(pairs) == 2
    over = compute_over_blocking(latest_validation(pairs), root=tree)
    assert (over.siblings_failed, over.siblings_run) == (0, 1)
    assert over.sources == ["later/repair_validation.json"]


def test_cost_of_learning_sums_irreversible_actions_and_money(tree: Path) -> None:
    validation = latest_validation(find_repair_validations(tree))
    assert validation is not None
    cost = compute_cost_of_learning(validation[1], root=tree)
    # 1 + 2 + 0 successful irreversible calls; the errored and read-only ones
    # in each trace are excluded.
    assert cost.irreversible_actions == 3
    assert cost.money_moved_usd == 160.0  # 100.0 + 50.5 + 9.5
    assert cost.validation_runs == 3
    assert cost.runs_not_retained == []


def test_cost_of_learning_counts_a_shared_rerun_once(tmp_path: Path) -> None:
    """Two controls can name the same sibling. Its refund only happened once."""
    _write(
        tmp_path / "repair_validation.json",
        {
            "run_id": "r",
            "test_name": "t",
            "controls": [
                {
                    "control": MATERIAL,
                    "verdict": "accepted",
                    "sibling_reruns": [_rerun("shared", "PASS")],
                },
                {
                    "control": PAPER,
                    "verdict": "skipped",
                    "sibling_reruns": [_rerun("shared", "PASS")],
                },
            ],
        },
    )
    _run_dir(tmp_path, "shared", refunds=[42.0], irreversible=1)
    validation = RepairValidation.model_validate_json(
        (tmp_path / "repair_validation.json").read_text(encoding="utf-8")
    )
    cost = compute_cost_of_learning(validation, root=tmp_path)
    assert (cost.validation_runs, cost.irreversible_actions, cost.money_moved_usd) == (1, 1, 42.0)


def test_a_rerun_that_was_not_retained_is_named_and_excluded(tmp_path: Path) -> None:
    _write(
        tmp_path / "repair_validation.json",
        {
            "run_id": "r",
            "test_name": "t",
            "controls": [
                {
                    "control": MATERIAL,
                    "verdict": "accepted",
                    "sibling_reruns": [_rerun("gone", "PASS")],
                }
            ],
        },
    )
    validation = RepairValidation.model_validate_json(
        (tmp_path / "repair_validation.json").read_text(encoding="utf-8")
    )
    cost = compute_cost_of_learning(validation, root=tmp_path)
    assert cost.runs_not_retained == ["gone"]
    assert (cost.validation_runs, cost.money_moved_usd) == (0, 0.0)


def test_a_malformed_artifact_is_skipped_rather_than_raising(tree: Path) -> None:
    """The writer runs after a merge. One bad file must not cost the record."""
    (tree / "broken").mkdir()
    (tree / "broken" / "repair_validation.json").write_text("{not json", encoding="utf-8")
    assert len(find_repair_validations(tree)) == 1


def test_snapshot_carries_every_measure_with_its_denominator(tree: Path) -> None:
    snapshot = build_snapshot(tree, commit="abc123")
    assert snapshot.commit == "abc123"
    assert snapshot.suite_pass_rate.numerator == 7
    assert snapshot.suite_pass_rate.denominator == 10
    assert snapshot.verified_failures == 3
    assert snapshot.coverage.accepted_over_prescribed.denominator == 2
    assert snapshot.over_blocking.rate.denominator == 2
    assert snapshot.cost_of_learning.money_moved_usd == 160.0


def test_history_appends_one_line_per_commit(tmp_path: Path, tree: Path) -> None:
    path = tmp_path / "out" / "metrics_history.jsonl"
    first = build_snapshot(tree, commit="c1")
    assert append_snapshot(path, first) is True
    assert append_snapshot(path, build_snapshot(tree, commit="c2")) is True
    assert len(load_history(path)) == 2


def test_appending_a_commit_already_recorded_is_refused(tmp_path: Path, tree: Path) -> None:
    """A re-run workflow must not put two points on one x."""
    path = tmp_path / "metrics_history.jsonl"
    append_snapshot(path, build_snapshot(tree, commit="c1"))
    assert append_snapshot(path, build_snapshot(tree, commit="c1")) is False
    assert len(load_history(path)) == 1


def test_history_round_trips_through_the_file(tmp_path: Path, tree: Path) -> None:
    path = tmp_path / "metrics_history.jsonl"
    written = build_snapshot(tree, commit="c1")
    append_snapshot(path, written)
    assert load_history(path)[0].model_dump() == written.model_dump()


def test_a_missing_history_file_reads_as_empty(tmp_path: Path) -> None:
    assert load_history(tmp_path / "nothing.jsonl") == []


def test_the_committed_history_file_is_readable() -> None:
    """Whatever main has recorded so far must still parse against this schema."""
    for snapshot in load_history(REPO_ROOT / "docs" / "acceptance" / "metrics_history.jsonl"):
        assert isinstance(snapshot, MetricsSnapshot)
        assert snapshot.commit


def test_a_hand_edited_rate_in_the_history_is_ignored() -> None:
    """The two counts are the record. A rate typed over them is not."""
    line = json.dumps(
        {
            "commit": "c1",
            "coverage": {
                "prescribed": 4,
                "materializable": 1,
                "validated": 1,
                "accepted": 1,
                "accepted_over_prescribed": {"numerator": 1, "denominator": 4, "value": 0.99},
                "materializable_over_prescribed": {"numerator": 1, "denominator": 4},
            },
            "over_blocking": {
                "siblings_run": 0,
                "siblings_failed": 0,
                "rate": {"numerator": 0, "denominator": 0},
            },
            "cost_of_learning": {
                "validation_runs": 0,
                "irreversible_actions": 0,
                "money_moved_usd": 0.0,
            },
            "suite_pass_rate": {"numerator": 1, "denominator": 2},
            "verified_failures": 0,
        }
    )
    snapshot = MetricsSnapshot.model_validate_json(line)
    assert snapshot.coverage.accepted_over_prescribed.value == 0.25


def test_scratch_artifacts_under_the_runs_dir_are_not_recorded(tree: Path) -> None:
    """The gate writes replay artifacts while it runs. Those are throwaway.

    Recording them would name a source file that stops existing the moment the
    runs directory is cleaned, and would report numbers from a validation the
    commit does not carry.
    """
    scratch = tree / "runs" / "collection-x" / "repair_validation.json"
    _write(
        scratch,
        {
            "run_id": "scratch",
            "test_name": "scratch",
            "batch_id": "batch_29990101T000000Z_zzzz",
            "controls": [
                {
                    "control": MATERIAL,
                    "verdict": "accepted",
                    "sibling_reruns": [_rerun("rr_scratch", "FAIL")],
                }
            ],
        },
    )
    assert len(find_repair_validations(tree)) == 2
    assert len(find_repair_validations(tree, exclude=[tree / "runs"])) == 1

    snapshot = build_snapshot(tree, commit="c1", exclude=[tree / "runs"])
    assert snapshot.over_blocking.sources == ["evidence/repair_validation.json"]
    # The committed artifacts still count. Excluding scratch must not also
    # empty the denominators.
    assert snapshot.coverage.prescribed == 2
    assert snapshot.suite_pass_rate.denominator == 10


def test_the_collector_appends_a_snapshot_and_keeps_its_own_exit_code(
    tmp_path: Path, tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The gate's verdict is the gate's. Recording a number must not change it."""
    from trace_harness.cli import main

    history = tmp_path / "history" / "metrics_history.jsonl"
    argv = [
        "--runs-dir",
        str(tmp_path / "runs"),
        "collect-regressions",
        str(tree / "acceptance"),
        "--append-history",
        str(history),
        "--commit",
        "abc123",
        "--history-root",
        str(tree),
    ]
    assert main(argv) == 0
    assert "Metrics history:" in capsys.readouterr().out

    recorded = load_history(history)
    assert [s.commit for s in recorded] == ["abc123"]
    assert recorded[0].coverage.prescribed == 2

    # Re-running the workflow on the same commit leaves one point on that x.
    assert main(argv) == 0
    assert len(load_history(history)) == 1


def test_the_collector_does_not_touch_the_history_without_the_flag(
    tmp_path: Path, tree: Path
) -> None:
    from trace_harness.cli import main

    history = tmp_path / "metrics_history.jsonl"
    assert (
        main(
            [
                "--runs-dir",
                str(tmp_path / "runs"),
                "collect-regressions",
                str(tree / "acceptance"),
            ]
        )
        == 0
    )
    assert not history.exists()
