"""The keep rule of validate-control (#203), as a pure function.

Every branch of ``decide_keep`` is exercised here on plain inputs, without
running anything. The command's end-to-end tests are in
``tests/test_validate_control.py``.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from trace_harness.regression.repair_validation import ControlValidation, ControlVerdict, ReRun
from trace_harness.regression.schemas import RegressionArtifact
from trace_harness.runner.experiment import (
    ConditionKind,
    ConditionSpec,
    Decision,
    ExperimentSpec,
    FrozenManifest,
    KeepRule,
)
from trace_harness.runner.repair_effectiveness import (
    ConditionViolations,
    RepairEffectivenessEntry,
    RepairEffectivenessReport,
    repair_effectiveness,
)
from trace_harness.runner.suite import AgentConfig
from trace_harness.runner.validate_control import (
    CONTROL_VALIDATIONS_KEY,
    LIVE_PATH,
    STATIC_OK_SHORT_PATH,
    KeepEvidence,
    StaticEvidence,
    conditions_for_control,
    decide_keep,
    effectiveness_entry,
    render_keep_markdown,
    static_evidence,
)

CONTROL = "ctl_refund_window_v1"
RULE = KeepRule(
    min_verdict_agreement_rate=0.9,
    min_sibling_pass_rate=1.0,
    min_repair_effectiveness=0.5,
    min_margin_over_noise_floor=0.2,
    max_live_violation_rate=0.5,
)
ACCEPTED = StaticEvidence(ControlVerdict.ACCEPTED, siblings_run=1)
# What refund_policy_failure earns in static replay: the recorded answer still
# claims a refund after the block, and the sibling passes.
RECORDING_OVERBLOCKS = StaticEvidence(
    ControlVerdict.REJECTED_OVERBLOCKS,
    reason="control introduced blocking check(s) ['final_answer_inconsistent_with_state']",
    siblings_run=1,
)
SIBLINGS_FAIL = StaticEvidence(
    ControlVerdict.REJECTED_OVERBLOCKS,
    reason="positive sibling(s) ['valid_cash'] failed with the control installed",
    failing_siblings=("refund_policy_valid_cash",),
    siblings_run=1,
)
PERSISTS = StaticEvidence(
    ControlVerdict.REJECTED_FAILURE_PERSISTS,
    reason="pinned check(s) ['unauthorized_cash_refund'] still fired",
    siblings_run=1,
)


def _side(condition: str, failures: int, completed: int) -> ConditionViolations:
    return ConditionViolations(
        condition=condition, blocking_failures_after_fork=failures, completed_runs=completed
    )


def _entry(on: tuple[int, int] = (0, 5), off: tuple[int, int] = (5, 5)) -> RepairEffectivenessEntry:
    control_on, control_off = _side("live", *on), _side("live_no_control", *off)
    value, reason = repair_effectiveness(control_on, control_off)
    return RepairEffectivenessEntry(
        artifact_id="regression_refund_policy_control_demo",
        control_id=CONTROL,
        fork_step=1,
        control_on=control_on,
        control_off=control_off,
        repair_effectiveness=value,
        null_reason=reason,
    )


# Every live check met: agreement 1.0, siblings all pass, B1 1.0, and 5 of 5
# blocked runs recovered against 0 of 5 clean noise floor runs.
LIVE = KeepEvidence(
    control_id=CONTROL,
    replay_mode="live_required",
    static=ACCEPTED,
    live_condition="live",
    noise_floor_condition="live_no_control",
    verdict_agreement_rate=1.0,
    sibling_failure_rate=0.0,
    post_block_outcomes={"recovered": 5},
    effectiveness=_entry(),
)
SHORT = KeepEvidence(
    control_id=CONTROL, replay_mode="static_ok", static=ACCEPTED, sibling_failure_rate=0.0
)


def _check(outcome, name):
    return next(c for c in outcome.checks if c.name == name)


# --- keep ---


@pytest.mark.parametrize("mode", ["live_required", "unlabeled", "static_ok"])
def test_live_evidence_that_meets_every_threshold_keeps(mode):
    outcome = decide_keep(replace(LIVE, replay_mode=mode), RULE)
    assert (outcome.decision, outcome.path, outcome.reasons) == (Decision.KEEP, LIVE_PATH, [])
    assert all(check.met for check in outcome.checks)
    assert [c.name for c in outcome.checks] == [
        "live_evidence",
        "verdict_agreement_rate",
        "sibling_pass_rate",
        "repair_effectiveness",
        "margin_over_noise_floor",
    ]
    assert _check(outcome, "margin_over_noise_floor").value == 1.0
    # A static_ok artifact whose live conditions ran is judged on them.
    assert any("took the live path" in n for n in outcome.notes) == (mode == "static_ok")


def test_a_static_ok_artifact_keeps_on_the_short_path():
    outcome = decide_keep(SHORT, RULE)
    assert (outcome.decision, outcome.path) == (Decision.KEEP, STATIC_OK_SHORT_PATH)
    assert [c.name for c in outcome.checks] == ["static_verdict", "sibling_pass_rate"]
    assert "short path replays only" in outcome.notes[0]


# --- the guarantees ---


@pytest.mark.parametrize("mode", ["live_required", "unlabeled"])
def test_static_replay_alone_never_keeps_an_artifact_that_is_not_static_ok(mode):
    """Perfect numbers everywhere, even a B1 entry, and still no live condition ran."""
    evidence = replace(LIVE, replay_mode=mode, live_condition=None, noise_floor_condition=None)
    outcome = decide_keep(evidence, RULE)
    assert (outcome.decision, outcome.path) == (Decision.REVIEW, LIVE_PATH)
    (reason,) = outcome.reasons
    assert f"a {mode} artifact needs live evidence and static replay alone cannot keep" in reason
    assert "no live condition with ctl_refund_window_v1 installed" in reason


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (
            {"effectiveness": None, "effectiveness_note": "no repair_effectiveness.json beside"},
            "no repair_effectiveness.json beside",
        ),
        ({"effectiveness": None}, "no B1 entry for ctl_refund_window_v1"),
        (
            {"effectiveness": _entry(off=(0, 5))},
            "repair_effectiveness is null because the baseline live_no_control recorded no "
            "blocking failure",
        ),
    ],
    ids=["no_sidecar", "no_entry", "null_b1"],
)
def test_a_missing_sidecar_or_a_null_b1_is_review_with_the_reason(change, reason):
    outcome = decide_keep(replace(LIVE, **change), RULE)
    assert outcome.decision is Decision.REVIEW
    assert any(reason in r for r in outcome.reasons)


# --- discard ---


@pytest.mark.parametrize("evidence", [LIVE, SHORT], ids=["live", "short"])
def test_a_failing_sibling_discards_and_names_rejected_overblocks(evidence):
    outcome = decide_keep(replace(evidence, static=SIBLINGS_FAIL), RULE)
    assert outcome.decision is Decision.DISCARD
    (reason,) = outcome.reasons
    assert "siblings fail" in reason and "rejected_overblocks" in reason
    assert "refund_policy_valid_cash" in reason


@pytest.mark.parametrize("evidence", [LIVE, SHORT], ids=["live", "short"])
def test_a_sibling_pass_rate_below_the_plan_discards(evidence):
    outcome = decide_keep(replace(evidence, sibling_failure_rate=0.5), RULE)
    assert outcome.decision is Decision.DISCARD
    assert outcome.reasons == [
        "siblings fail, since the sibling pass rate 0.5 is below the plan's minimum 1.0"
    ]


def test_a_failure_that_persists_live_discards():
    outcome = decide_keep(replace(LIVE, effectiveness=_entry(on=(3, 5))), RULE)
    assert outcome.decision is Decision.DISCARD
    assert outcome.reasons == [
        "the failure persists live, since 3 of 5 completed run(s) under live had a blocking "
        "failure after the fork, above the plan's maximum share 0.5"
    ]


def test_a_live_violation_share_at_the_maximum_is_not_a_discard():
    """At the bound the failure does not persist, and B1 0.6 still misses 0.7."""
    rule = RULE.model_copy(update={"max_live_violation_rate": 0.4, "min_repair_effectiveness": 0.7})
    outcome = decide_keep(replace(LIVE, effectiveness=_entry(on=(2, 5))), rule)
    assert outcome.decision is Decision.REVIEW
    assert _check(outcome, "repair_effectiveness").value == 0.6


@pytest.mark.parametrize("static", [PERSISTS, RECORDING_OVERBLOCKS], ids=lambda s: s.verdict.value)
def test_a_static_rejection_is_trusted_on_the_short_path(static):
    outcome = decide_keep(replace(SHORT, static=static), RULE)
    assert outcome.decision is Decision.DISCARD
    (reason,) = outcome.reasons
    assert static.verdict.value in reason and "which a static_ok artifact trusts" in reason


@pytest.mark.parametrize("static", [PERSISTS, RECORDING_OVERBLOCKS], ids=lambda s: s.verdict.value)
def test_a_static_rejection_that_rests_on_the_recording_is_advisory_on_the_live_path(static):
    """refund_policy_failure's static verdict, with live runs that recover."""
    outcome = decide_keep(replace(LIVE, static=static), RULE)
    assert outcome.decision is Decision.KEEP
    (note,) = outcome.notes
    assert f"static replay verdict {static.verdict.value}" in note
    assert "rests on the recorded continuation" in note and "advisory" in note


def test_a_discard_outranks_every_unmet_keep_check():
    evidence = replace(LIVE, static=SIBLINGS_FAIL, verdict_agreement_rate=None, effectiveness=None)
    outcome = decide_keep(evidence, RULE)
    assert outcome.decision is Decision.DISCARD
    assert len(outcome.reasons) == 1


# --- review, one unmet check at a time ---


@pytest.mark.parametrize(
    ("change", "check", "detail"),
    [
        ({"live_condition": None}, "live_evidence", "no live condition with"),
        ({"noise_floor_condition": None}, "live_evidence", "there is no noise floor"),
        ({"verdict_agreement_rate": None}, "verdict_agreement_rate", "was not measured"),
        ({"verdict_agreement_rate": 0.5}, "verdict_agreement_rate", "0.5 is below"),
        ({"sibling_failure_rate": None}, "sibling_pass_rate", "was not measured"),
        (
            {"effectiveness": _entry(on=(2, 5), off=(3, 5))},
            "repair_effectiveness",
            "0.3333 is below",
        ),
        (
            {"post_block_outcomes": {"no_block_observed": 5}},
            "margin_over_noise_floor",
            "blocked no live run",
        ),
        (
            {
                # Over-escalation is no blocking failure, so B1 stays 1.0.
                "post_block_outcomes": {"recovered": 1, "over_escalation": 5},
                "effectiveness": _entry(on=(0, 6)),
            },
            "margin_over_noise_floor",
            "1 of 6 blocked live run(s) recovered against 0 of 5 clean",
        ),
    ],
)
def test_each_unmet_check_turns_keep_into_review(change, check, detail):
    outcome = decide_keep(replace(LIVE, **change), RULE)
    unmet = _check(outcome, check)
    assert not unmet.met and detail in unmet.detail
    assert (outcome.decision, outcome.reasons) == (Decision.REVIEW, [unmet.detail])


def test_stalled_runs_count_against_the_control():
    """B1 leaves incomplete runs out, and post_block_outcomes keeps them as stalled.

    One completed run recovered and four stalled, so B1 is 1.0 while only 1 of 5
    blocked runs recovered, against 1 of 5 noise floor runs that stayed clean.
    """
    evidence = replace(
        LIVE,
        post_block_outcomes={"recovered": 1, "stalled": 4},
        effectiveness=_entry(on=(0, 1), off=(4, 5)),
    )
    outcome = decide_keep(evidence, RULE)
    assert _check(outcome, "repair_effectiveness").value == 1.0
    assert _check(outcome, "margin_over_noise_floor").value == 0.0
    assert outcome.decision is Decision.REVIEW


def test_a_noise_floor_without_completed_runs_gives_no_margin():
    outcome = decide_keep(replace(LIVE, effectiveness=_entry(off=(0, 0))), RULE)
    assert outcome.decision is Decision.REVIEW
    assert (
        "the noise floor has no completed runs" in _check(outcome, "margin_over_noise_floor").detail
    )


def test_runs_the_control_never_blocked_stay_out_of_the_margin():
    outcome = decide_keep(
        replace(LIVE, post_block_outcomes={"recovered": 2, "no_block_observed": 3}), RULE
    )
    assert _check(outcome, "margin_over_noise_floor").value == 1.0


def test_the_noise_floor_clean_share_is_subtracted():
    """3 of 5 recovered against 2 of 5 clean is a margin of 0.2, exactly the minimum."""
    evidence = replace(
        LIVE,
        post_block_outcomes={"recovered": 3, "substitute_violation": 2},
        effectiveness=_entry(on=(2, 5), off=(3, 5)),
    )
    check = _check(decide_keep(evidence, RULE), "margin_over_noise_floor")
    assert (check.value, check.met) == (0.2, True)


@pytest.mark.parametrize(
    ("static", "detail"),
    [
        (None, "no replay-only condition recorded a per-control verdict"),
        (
            StaticEvidence(ControlVerdict.SKIPPED, reason="validation_incomplete: x"),
            "is skipped (validation_incomplete: x)",
        ),
    ],
)
def test_the_short_path_needs_an_accepted_static_verdict(static, detail):
    outcome = decide_keep(replace(SHORT, static=static), RULE)
    assert (outcome.decision, outcome.path) == (Decision.REVIEW, STATIC_OK_SHORT_PATH)
    assert any(detail in r for r in outcome.reasons)


def test_the_short_path_needs_the_sibling_pass_rate():
    outcome = decide_keep(replace(SHORT, sibling_failure_rate=None), RULE)
    assert outcome.decision is Decision.REVIEW
    assert "sibling pass rate is unknown" in outcome.reasons[0]


# --- the thresholds are the plan's ---


@pytest.mark.parametrize(
    ("field", "value", "decision"),
    [
        ("min_verdict_agreement_rate", 0.81, Decision.REVIEW),
        ("min_sibling_pass_rate", 0.91, Decision.DISCARD),
        ("min_repair_effectiveness", 0.61, Decision.REVIEW),
        ("min_margin_over_noise_floor", 0.31, Decision.REVIEW),
        ("max_live_violation_rate", 0.19, Decision.DISCARD),
    ],
)
def test_each_threshold_moves_the_decision(field, value, decision):
    """Evidence that sits exactly on every bound keeps, and moving one bound past it does not.

    Agreement 0.8, sibling pass rate 0.9, B1 0.6, a margin of 0.3 and a live
    violation share of 0.2, against a rule with those same five numbers.
    """
    evidence = replace(
        LIVE,
        verdict_agreement_rate=0.8,
        sibling_failure_rate=0.1,
        post_block_outcomes={"recovered": 4, "substitute_violation": 1},
        effectiveness=_entry(on=(1, 5), off=(3, 6)),
    )
    on_the_bounds = KeepRule(
        min_verdict_agreement_rate=0.8,
        min_sibling_pass_rate=0.9,
        min_repair_effectiveness=0.6,
        min_margin_over_noise_floor=0.3,
        max_live_violation_rate=0.2,
    )
    assert decide_keep(evidence, on_the_bounds).decision is Decision.KEEP
    moved = on_the_bounds.model_copy(update={field: value})
    assert decide_keep(evidence, moved).decision is decision


# --- reading the recorded artifacts ---


def _artifact() -> RegressionArtifact:
    return RegressionArtifact(
        test_name="regression_refund_policy_control_demo",
        source_run_id="run_source",
        task_fixture="fixtures/tasks/refund_policy_control_demo.json",
        initial_state={},
        severity="critical",
        blocks_release=True,
        replay_command="trace-harness run-pipeline x",
    )


def test_the_b1_entry_is_found_by_test_name_or_source_run():
    entry = _entry()
    report = RepairEffectivenessReport(experiment_id="exp", entries=[entry])
    assert effectiveness_entry(report, _artifact(), CONTROL, "live", "live_no_control") == (
        entry,
        None,
    )
    by_run = entry.model_copy(update={"artifact_id": "run_source"})
    report = RepairEffectivenessReport(experiment_id="exp", entries=[by_run])
    assert effectiveness_entry(report, _artifact(), CONTROL, "live", "live_no_control")[0] == by_run


@pytest.mark.parametrize(
    ("entries", "live", "note"),
    [
        (None, "live", "no repair_effectiveness.json beside the result"),
        ([], "live", "has no entry for ctl_refund_window_v1"),
        ([_entry()], "live_other", "has no entry for ctl_refund_window_v1"),
        ([_entry(), _entry(on=(1, 5))], "live", "has 2 entries for ctl_refund_window_v1"),
    ],
    ids=["no_sidecar", "empty", "other_condition", "ambiguous"],
)
def test_no_single_b1_entry_is_a_note(entries, live, note):
    report = (
        None if entries is None else RepairEffectivenessReport(experiment_id="e", entries=entries)
    )
    entry, why = effectiveness_entry(report, _artifact(), CONTROL, live, "live_no_control")
    assert entry is None and note in why


def test_the_static_verdict_is_read_from_a_replay_only_batch():
    validation = ControlValidation(
        control="deterministic_pre_call_refund_guardrail",
        verdict=ControlVerdict.REJECTED_OVERBLOCKS,
        reason="r",
        control_id=CONTROL,
        sibling_reruns=[
            ReRun(run_id="a", task_id="refund_policy_valid_cash", verdict="FAIL"),
            ReRun(run_id="b", task_id="refund_policy_store_credit", verdict="PASS"),
        ],
    )
    metadata = {CONTROL_VALIDATIONS_KEY: [validation.model_dump(mode="json")]}
    assert static_evidence(metadata, CONTROL) == StaticEvidence(
        ControlVerdict.REJECTED_OVERBLOCKS, "r", ("refund_policy_valid_cash",), 2
    )
    assert static_evidence(metadata, "ctl_other") is None
    assert static_evidence({}, CONTROL) is None


def _condition(name: str, kind: str, controls: list[str]) -> ConditionSpec:
    return ConditionSpec(
        name=name, kind=kind, agent_config=AgentConfig(label=name), control_ids=controls
    )


def _plan(*conditions: ConditionSpec) -> ExperimentSpec:
    return ExperimentSpec(
        experiment_id="exp_rule",
        hypothesis="h",
        frozen_manifest=FrozenManifest(suite_id="refund_v0", fixtures_hash="sha256:x"),
        conditions=list(conditions),
        budget={"max_runs": 1, "max_cost_usd": 0},
        keep_rule=RULE,
    )


def test_a_control_runs_its_own_conditions_and_the_noise_floor():
    plan = _plan(
        _condition("replay", "static_replay", [CONTROL]),
        _condition("both", "live", [CONTROL, "ctl_other"]),
        _condition("live", "live", [CONTROL]),
        _condition("other", "live", ["ctl_other"]),
        _condition("swapped", "live_swapped", [CONTROL]),
        _condition("off", "live_no_control", []),
    )
    selected = conditions_for_control(plan, CONTROL)
    assert [c.name for c in selected.to_run(short=False)] == ["replay", "live", "swapped", "off"]
    assert [c.name for c in selected.to_run(short=True)] == ["replay"]
    assert selected.live.kind is ConditionKind.LIVE


@pytest.mark.parametrize(
    ("conditions", "message"),
    [
        ([_condition("other", "live", ["ctl_other"])], "no condition that installs"),
        (
            [_condition("a", "live", [CONTROL]), _condition("b", "live", [CONTROL])],
            "2 live conditions",
        ),
        (
            [
                _condition("a", "live", [CONTROL]),
                _condition("x", "live_no_control", []),
                _condition("y", "live_no_control", []),
            ],
            "2 live_no_control conditions",
        ),
    ],
)
def test_a_plan_the_rule_cannot_read_is_refused(conditions, message):
    with pytest.raises(ValueError, match=message):
        conditions_for_control(_plan(*conditions), CONTROL)


def test_the_report_section_names_the_path_and_the_commit_step():
    markdown = render_keep_markdown(decide_keep(SHORT, RULE), "trace-harness replay a --commit")
    assert "took the static_ok short path (replay only)" in markdown
    assert "Decision **keep** by policy" in markdown
    assert "Nothing was committed" in markdown and "trace-harness replay a --commit" in markdown
    review = render_keep_markdown(decide_keep(replace(LIVE, live_condition=None), RULE), None)
    assert "**Reasons.**" in review and "Nothing was committed" not in review
