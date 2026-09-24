"""The keep rule of validate-control (#203), as a pure function.

Every branch of ``decide_keep`` is exercised here on plain inputs, without
running anything. The command's end-to-end tests are in
``tests/test_validate_control.py``.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from trace_harness.regression.repair_validation import ControlValidation, ControlVerdict, ReRun
from trace_harness.regression.schemas import RegressionArtifact, ReplayModeBasis
from trace_harness.runner.batch import BatchRunEntry, BatchSummary, aggregate_entries
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
    live_arm,
    recovered_with_blocking_failure,
    render_keep_markdown,
    short_path_for,
    static_evidence,
)
from trace_harness.verifiers.base import FailedCheck, VerifierResult

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


BATCHES = {"live": "batch_on", "live_no_control": "batch_off"}


def _side(condition: str, failures: int, completed: int) -> ConditionViolations:
    return ConditionViolations(
        condition=condition,
        batch_id=BATCHES[condition],
        blocking_failures_after_fork=failures,
        completed_runs=completed,
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
    recovered_with_blocking_failure=0,
    effectiveness=_entry(),
)
SHORT = KeepEvidence(
    control_id=CONTROL,
    replay_mode="static_ok",
    short_path=True,
    static=ACCEPTED,
    sibling_failure_rate=0.0,
)
LIVE_PATH_NOTE = (
    "a static_ok artifact took the live path, since the plan declares no replay-only condition "
    "for ctl_refund_window_v1"
)


def _check(outcome, name):
    return next(c for c in outcome.checks if c.name == name)


# --- keep ---


@pytest.mark.parametrize("mode", ["live_required", "unlabeled", "static_ok"])
def test_live_evidence_that_meets_every_threshold_keeps(mode):
    note = LIVE_PATH_NOTE if mode == "static_ok" else None
    outcome = decide_keep(replace(LIVE, replay_mode=mode, path_note=note), RULE)
    assert (outcome.decision, outcome.path, outcome.reasons) == (Decision.KEEP, LIVE_PATH, [])
    assert all(check.met for check in outcome.checks)
    assert [c.name for c in outcome.checks] == [
        "live_evidence",
        "static_verdict",
        "verdict_agreement_rate",
        "sibling_pass_rate",
        "repair_effectiveness",
        "margin_over_noise_floor",
    ]
    assert _check(outcome, "margin_over_noise_floor").value == 1.0
    # A static_ok artifact that took the live path says why.
    assert outcome.notes == ([LIVE_PATH_NOTE] if note else [])


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
        "siblings fail, since the sibling pass rate 0.5 is below the plan's minimum 1.0, and "
        "siblings have zero tolerance"
    ]


@pytest.mark.parametrize("evidence", [LIVE, SHORT], ids=["live", "short"])
def test_no_sibling_failure_rate_is_small_enough_to_round_away(evidence):
    """One failing sibling in 25,000 is a pass rate of 0.99996, which four places would call 1.0."""
    outcome = decide_keep(replace(evidence, sibling_failure_rate=0.00004), RULE)
    assert outcome.decision is Decision.DISCARD
    check = _check(outcome, "sibling_pass_rate")
    assert (check.value, check.met) == (0.99996, False)
    assert outcome.reasons == [
        "siblings fail, since the sibling pass rate 0.99996 is below the plan's minimum 1.0, and "
        "siblings have zero tolerance"
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
def test_a_static_rejection_on_the_live_path_is_review_and_never_a_commit(static):
    """refund_policy_failure's static verdict, with live runs that recover.

    The replay did not clear while the live majority did, so agreement on the
    pair is 0, as #200 derives it. The rejection rests on the recording, so it
    does not discard, and ``replay --apply-control --commit`` would refuse the
    control, so it cannot keep.
    """
    outcome = decide_keep(replace(LIVE, static=static, verdict_agreement_rate=0.0), RULE)
    assert outcome.decision is Decision.REVIEW
    verdict = _check(outcome, "static_verdict")
    assert (verdict.value, verdict.met) == (static.verdict.value, False)
    assert outcome.reasons == [
        f"static replay verdict for {CONTROL} is {static.verdict.value} ({static.reason}), and "
        "replay --apply-control --commit commits only an accepted control",
        "verdict_agreement_rate 0.0 is below the plan's minimum 0.9",
    ]
    (note,) = outcome.notes
    assert f"static replay verdict {static.verdict.value}" in note
    assert "rests on the recorded continuation" in note and "does not discard" in note


def test_the_static_verdict_alone_keeps_a_live_path_control_from_keep():
    """Every live number stated perfect, and the replay the commit step runs rejected it."""
    outcome = decide_keep(replace(LIVE, static=RECORDING_OVERBLOCKS), RULE)
    assert outcome.decision is Decision.REVIEW
    assert [c.name for c in outcome.checks if not c.met] == ["static_verdict"]


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
        (
            {"live_condition": None, "live_gap": "live is a fixture arm with no script"},
            "live_evidence",
            "yet live is a fixture arm with no script",
        ),
        (
            {"noise_floor_condition": None, "noise_floor_gap": "live_no_control was skipped (x)"},
            "live_evidence",
            "yet live_no_control was skipped (x)",
        ),
        ({"static": None}, "static_verdict", "no replay-only condition recorded"),
        (
            {"static": StaticEvidence(ControlVerdict.SKIPPED, reason="not_materializable")},
            "static_verdict",
            "is skipped (not_materializable), and replay --apply-control --commit commits only",
        ),
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
            "1 of 6 blocked live run(s) recovered with no blocking failure after the fork, "
            "against 0 of 5 clean",
        ),
        (
            {"recovered_with_blocking_failure": None},
            "margin_over_noise_floor",
            "were not checked for a blocking failure after the fork",
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

    Five completed runs recovered and fifteen stalled, so B1 is 1.0 while only 5
    of 20 blocked runs recovered, against 1 of 5 noise floor runs that stayed
    clean. Five completed runs on each side is the least #200 computes B1 on.
    """
    evidence = replace(
        LIVE,
        post_block_outcomes={"recovered": 5, "stalled": 15},
        effectiveness=_entry(on=(0, 5), off=(4, 5)),
    )
    outcome = decide_keep(evidence, RULE)
    assert _check(outcome, "repair_effectiveness").value == 1.0
    assert _check(outcome, "margin_over_noise_floor").value == 0.05
    assert outcome.decision is Decision.REVIEW
    assert [c.name for c in outcome.checks if not c.met] == ["margin_over_noise_floor"]


def test_a_noise_floor_without_completed_runs_gives_no_margin():
    outcome = decide_keep(replace(LIVE, effectiveness=_entry(off=(0, 0))), RULE)
    assert outcome.decision is Decision.REVIEW
    assert (
        "the noise floor has no completed runs" in _check(outcome, "margin_over_noise_floor").detail
    )


def test_a_recovered_run_that_still_failed_after_the_fork_counts_against_the_control():
    """The noise floor's clean share already counts such a run as unclean, and now so does this one.

    Two of five recovered runs missed a check the post-block classifier does not
    map. B1 0.6 and a live share of 0.4 pass, and the margin is 3/5 - 0/5.
    """
    evidence = replace(LIVE, recovered_with_blocking_failure=2, effectiveness=_entry(on=(2, 5)))
    rule = RULE.model_copy(update={"min_margin_over_noise_floor": 0.7})
    outcome = decide_keep(evidence, rule)
    check = _check(outcome, "margin_over_noise_floor")
    assert (check.value, check.met) == (0.6, False)
    assert "3 of 5 blocked live run(s) recovered with no blocking failure" in check.detail
    assert outcome.decision is Decision.REVIEW
    assert decide_keep(replace(evidence, recovered_with_blocking_failure=0), rule).decision is (
        Decision.KEEP
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


def test_a_value_just_below_its_bound_is_never_rounded_onto_it():
    """B1 0.49996 misses 0.5, and the check records the exact value so it never reads 0.5."""
    entry = _entry().model_copy(update={"repair_effectiveness": 0.49996})
    outcome = decide_keep(replace(LIVE, effectiveness=entry), RULE)
    check = _check(outcome, "repair_effectiveness")
    assert (check.value, check.met) == (0.49996, False)
    assert outcome.reasons == ["repair_effectiveness 0.49996 is below the plan's minimum 0.5"]
    assert (
        _check(
            decide_keep(
                replace(
                    LIVE,
                    effectiveness=_entry().model_copy(update={"repair_effectiveness": 0.333333333}),
                ),
                RULE,
            ),
            "repair_effectiveness",
        ).value
        == 0.3333
    )


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
        ("min_repair_effectiveness", 0.61, Decision.REVIEW),
        ("min_margin_over_noise_floor", 0.31, Decision.REVIEW),
        ("max_live_violation_rate", 0.19, Decision.DISCARD),
    ],
)
def test_each_threshold_that_can_move_moves_the_decision(field, value, decision):
    """Evidence that sits exactly on every bound keeps, and moving one bound past it does not.

    Agreement 0.8, every sibling passing, B1 0.6, a margin of 0.3 and a live
    violation share of 0.2, against a rule with those same numbers.
    min_sibling_pass_rate is fixed at 1.0, and the sibling tests above cover
    it.
    """
    evidence = replace(
        LIVE,
        verdict_agreement_rate=0.8,
        sibling_failure_rate=0.0,
        post_block_outcomes={"recovered": 4, "substitute_violation": 1},
        effectiveness=_entry(on=(1, 5), off=(3, 6)),
    )
    on_the_bounds = KeepRule(
        min_verdict_agreement_rate=0.8,
        min_sibling_pass_rate=1.0,
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
    found = effectiveness_entry(report, _artifact(), CONTROL, "live", "live_no_control", BATCHES)
    assert found == (entry, None)
    by_run = entry.model_copy(update={"artifact_id": "run_source"})
    report = RepairEffectivenessReport(experiment_id="exp", entries=[by_run])
    found = effectiveness_entry(report, _artifact(), CONTROL, "live", "live_no_control", BATCHES)
    assert found[0] == by_run


@pytest.mark.parametrize(
    "batches",
    [
        {"live": "batch_now_on", "live_no_control": "batch_off"},
        {"live": "batch_on", "live_no_control": "batch_now_off"},
        {},
    ],
    ids=["live_rerecorded", "noise_floor_rerecorded", "nothing_recorded"],
)
def test_an_entry_for_other_batches_is_stale(batches):
    """Names alone match a sidecar left from an earlier record of the same plan."""
    report = RepairEffectivenessReport(experiment_id="e", entries=[_entry()])
    entry, why = effectiveness_entry(
        report, _artifact(), CONTROL, "live", "live_no_control", batches
    )
    assert entry is None
    assert why == (
        "repair_effectiveness.json is stale, since its entry for ctl_refund_window_v1 on "
        "regression_refund_policy_control_demo compares batches batch_on and batch_off, and "
        f"this result recorded {batches.get('live')} and {batches.get('live_no_control')}"
    )


def test_an_entry_that_names_no_batch_is_never_this_results():
    """#200 leaves batch_id null when it pooled a side over several batches."""
    pooled = _entry().model_copy(
        update={"control_on": _entry().control_on.model_copy(update={"batch_id": None})}
    )
    report = RepairEffectivenessReport(experiment_id="e", entries=[pooled])
    entry, why = effectiveness_entry(
        report, _artifact(), CONTROL, "live", "live_no_control", BATCHES
    )
    assert entry is None and "is stale" in why


@pytest.mark.parametrize(
    ("entries", "live", "note"),
    [
        (None, "live", "no repair_effectiveness.json beside the result"),
        ([], "live", "has no entry for ctl_refund_window_v1"),
        ([_entry()], "live_other", "has no entry for ctl_refund_window_v1"),
        ([_entry()], None, "no live condition and noise floor pair that is live evidence"),
        ([_entry().model_copy(update={"control_id": "ctl_other"})], "live", "has no entry"),
        (
            [_entry().model_copy(update={"artifact_id": "regression_other_task"})],
            "live",
            "has no entry",
        ),
        ([_entry(), _entry(on=(1, 5))], "live", "has 2 entries for ctl_refund_window_v1"),
    ],
    ids=[
        "no_sidecar",
        "empty",
        "other_condition",
        "no_live_condition",
        "other_control",
        "other_artifact",
        "ambiguous",
    ],
)
def test_no_single_b1_entry_is_a_note(entries, live, note):
    report = (
        None if entries is None else RepairEffectivenessReport(experiment_id="e", entries=entries)
    )
    entry, why = effectiveness_entry(report, _artifact(), CONTROL, live, "live_no_control", BATCHES)
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


# --- which path, and which arms are live evidence ---


def _selected(*conditions: ConditionSpec):
    return conditions_for_control(_plan(*conditions), CONTROL)


def _labeled(mode: str, control_ids: list[str], **basis) -> RegressionArtifact:
    """An artifact whose basis supports static_ok unless ``basis`` changes a fact."""
    facts = {
        "control_ids": control_ids,
        "control_step": 2,
        "first_irreversible_action_step": 2,
        "rule_kind": "prohibition",
        "gated_tool": "issue_refund",
        "checks_reachable_via_gated_tool": ["unauthorized_cash_refund"],
        "checks_covered_by_control": ["unauthorized_cash_refund"],
        **basis,
    }
    return _artifact().model_copy(
        update={"replay_mode": mode, "replay_mode_basis": ReplayModeBasis(**facts)}
    )


REPLAY = _condition("replay", "static_replay", [CONTROL])
LIVE_ARM = _condition("live", "live", [CONTROL])


@pytest.mark.parametrize(
    ("artifact", "conditions", "short", "note"),
    [
        (_labeled("static_ok", [CONTROL]), (REPLAY, LIVE_ARM), True, None),
        (_labeled("live_required", [CONTROL]), (REPLAY, LIVE_ARM), False, None),
        (_labeled("unlabeled", [CONTROL]), (REPLAY,), False, None),
        (
            _labeled("static_ok", [CONTROL]),
            (LIVE_ARM,),
            False,
            "the plan declares no replay-only condition for ctl_refund_window_v1",
        ),
        (
            _labeled("static_ok", ["ctl_other"]),
            (REPLAY, LIVE_ARM),
            False,
            "its label was predicted with ['ctl_other'] installed and says nothing about "
            "ctl_refund_window_v1",
        ),
        (
            _artifact().model_copy(update={"replay_mode": "static_ok"}),
            (REPLAY,),
            False,
            "its replay_mode_basis does not support the label",
        ),
        (
            # Set by hand, since the control step comes after the first irreversible action.
            _labeled("static_ok", [CONTROL], first_irreversible_action_step=1),
            (REPLAY,),
            False,
            "its replay_mode_basis does not support the label",
        ),
    ],
    ids=[
        "static_ok",
        "live_required",
        "unlabeled",
        "no_replay",
        "other_control",
        "no_basis",
        "hand_set",
    ],
)
def test_one_function_chooses_the_path_before_anything_runs(artifact, conditions, short, note):
    """The command runs the path this returns and hands it to the rule, so the two never differ."""
    chosen, why = short_path_for(artifact, CONTROL, _selected(*conditions))
    assert chosen is short
    if note is None:
        assert why is None
    else:
        assert why.startswith("a static_ok artifact took the live path, since ") and note in why


def test_the_rule_judges_the_path_it_is_given():
    """A static_ok artifact with nothing live recorded stays on the live path it was given."""
    outcome = decide_keep(replace(SHORT, short_path=False, path_note=LIVE_PATH_NOTE), RULE)
    assert outcome.path == LIVE_PATH and outcome.decision is Decision.REVIEW
    assert _check(outcome, "live_evidence").met is False
    assert outcome.notes == [LIVE_PATH_NOTE]
    with pytest.raises(ValueError, match="only a static_ok artifact takes the short path"):
        replace(LIVE, short_path=True)


def _entry_row(**fields) -> BatchRunEntry:
    return BatchRunEntry(
        **{
            "run_id": "run_a",
            "task_id": "refund_policy_control_demo",
            "task_path": "fixtures/tasks/refund_policy_control_demo.json",
            "agent_label": "fixture",
            "provider": "fixture",
            "status": "completed",
            **fields,
        }
    )


def _batch(*entries: BatchRunEntry) -> BatchSummary:
    now = datetime(2026, 9, 24, tzinfo=UTC)
    return BatchSummary(
        batch_id="batch_x",
        suite_id="refund_v0",
        started_at=now,
        finished_at=now,
        agent_configs=[AgentConfig(label="fixture")],
        entries=list(entries),
        aggregates=aggregate_entries(list(entries)),
    )


def _arm(provider: str = "fixture", script: str | None = "s.json") -> ConditionSpec:
    return ConditionSpec(
        name="live",
        kind="live",
        agent_config=AgentConfig(label=provider, provider=provider),
        control_ids=[CONTROL],
        continuation_script=script,
    )


LEFT = _batch(_entry_row(first_post_fork_divergence_step=3))
STAYED = _batch(_entry_row(), _entry_row(status="terminated", first_post_fork_divergence_step=2))


@pytest.mark.parametrize(
    ("condition", "batch", "skipped", "expected"),
    [
        (_arm(), LEFT, None, ("live", None)),
        (_arm("gemini", None), STAYED, None, ("live", None)),
        (None, None, None, (None, None)),
        (_arm(), None, "no cassette recorded at x", "live was skipped (no cassette recorded at x)"),
        (_arm(), None, None, "live was not recorded"),
        (
            _arm(script=None),
            LEFT,
            None,
            "live is a fixture arm with no continuation_script, so it replays the recording",
        ),
        (
            _arm(),
            STAYED,
            None,
            "live is a fixture arm whose continuation_script never left the recording in a "
            "completed run",
        ),
    ],
    ids=["scripted", "live_model", "absent", "skipped", "unrecorded", "no_script", "repeats"],
)
def test_only_an_arm_that_can_react_to_the_block_is_live_evidence(
    condition, batch, skipped, expected
):
    found = live_arm(condition, batch, skipped)
    if isinstance(expected, tuple):
        assert found == expected
    else:
        assert found[0] is None and found[1].startswith(expected)


def _verdict(*checks: tuple[bool, list[int]]) -> VerifierResult:
    return VerifierResult(
        verifier_id="refund_policy",
        run_id="r",
        passed=not checks,
        failed_checks=[
            FailedCheck(
                check_id=f"c{i}",
                message="m",
                expected="e",
                actual="a",
                blocks_release=blocks,
                step_ids=steps,
            )
            for i, (blocks, steps) in enumerate(checks)
        ],
    )


def test_recovered_runs_are_read_for_blocking_failures_after_the_fork():
    verdicts = {
        "clean": _verdict(),
        "at_fork": _verdict((True, [1])),
        "not_blocking": _verdict((False, [3])),
        "after_fork": _verdict((True, [1, 3])),
    }
    batch = _batch(
        *(_entry_row(run_id=run_id, post_block_outcome="recovered") for run_id in verdicts),
        _entry_row(run_id="no_verdict", post_block_outcome="recovered"),
        _entry_row(run_id="stalled", post_block_outcome="stalled"),
    )
    # after_fork, and no_verdict, which nothing shows clean.
    assert recovered_with_blocking_failure(batch, verdicts.get, fork_step=1) == 2
    assert recovered_with_blocking_failure(batch, verdicts.get, fork_step=3) == 1


def test_more_recovered_failures_than_recovered_runs_is_refused():
    with pytest.raises(ValueError, match=r"6 recovered run\(s\) with a blocking failure, out of 3"):
        replace(LIVE, recovered_with_blocking_failure=6, post_block_outcomes={"recovered": 3})
