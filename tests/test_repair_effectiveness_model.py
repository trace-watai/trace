"""The B1 sidecar model and formula shared by #200 and #203."""

from __future__ import annotations

import pytest

from trace_harness.runner.repair_effectiveness import (
    REPAIR_EFFECTIVENESS_SCHEMA_VERSION,
    ConditionViolations,
    RepairEffectivenessEntry,
    RepairEffectivenessReport,
    repair_effectiveness,
)


def _side(condition: str, failures: int, completed: int) -> ConditionViolations:
    return ConditionViolations(
        condition=condition, blocking_failures_after_fork=failures, completed_runs=completed
    )


def test_b1_is_one_minus_the_ratio_of_violation_rates() -> None:
    value, reason = repair_effectiveness(_side("live", 1, 5), _side("live_no_control", 4, 5))
    assert value == pytest.approx(0.75)
    assert reason is None


def test_a_control_that_changes_nothing_scores_zero() -> None:
    value, _ = repair_effectiveness(_side("live", 3, 5), _side("live_no_control", 3, 5))
    assert value == pytest.approx(0.0)


@pytest.mark.parametrize(
    ("on", "off", "reason"),
    [
        ((0, 0), (4, 5), "no completed runs under live"),
        ((1, 5), (0, 0), "no completed runs under live_no_control"),
        ((0, 5), (0, 5), "the baseline live_no_control recorded no blocking failure"),
    ],
)
def test_b1_is_null_with_a_reason_when_the_ratio_has_no_meaning(on, off, reason) -> None:
    value, why = repair_effectiveness(_side("live", *on), _side("live_no_control", *off))
    assert value is None
    assert why == reason


def test_the_report_round_trips_and_pins_its_version() -> None:
    entry = RepairEffectivenessEntry(
        artifact_id="regression_refund_policy_failure",
        control_id="ctl_refund_window_v1",
        fork_step=5,
        model="gemini-3.6-flash",
        control_on=_side("live", 1, 5),
        control_off=_side("live_no_control", 4, 5),
        repair_effectiveness=0.75,
    )
    report = RepairEffectivenessReport(experiment_id="exp_001_replay_validity", entries=[entry])
    assert REPAIR_EFFECTIVENESS_SCHEMA_VERSION == "0.1.0"
    assert RepairEffectivenessReport.model_validate_json(report.model_dump_json()) == report


def test_negative_counts_are_rejected() -> None:
    with pytest.raises(ValueError):
        _side("live", -1, 5)
