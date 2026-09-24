"""Verdict agreement, sibling failures and B1 on a synthetic experiment computed by hand (#200).

Three artifacts share one control. The worked numbers are in the comments
beside each batch, so every assertion below can be checked with a pencil.

- Artifact A, fork 5. Static replay exits 0, so the static verdict is clear.
  Live control on, 5 completed seeds: 4 clear, 1 blocking after the fork, so
  the live verdict is clear (4/5 >= 1/2) and the pair agrees. Control off, 4 of
  5 blocking after the fork. B1 = 1 - (1/5) / (4/5) = 0.75.
- Artifact B, fork 3. Static exits 1, so it is not clear. Live control on,
  6 completed seeds with 3 clear, which is exactly half and therefore clear, so
  the pair disagrees. Control off never blocks after the fork, so B1 is null.
- Artifact D, fork 4. Static exits 1. Live has 4 completed seeds and a setup
  error, which is insufficient, so the pair is excluded. No control-off runs,
  so B1 is null.
- The swapped model on A blocks after the fork on 5 of 5, so it is not clear
  and disagrees with the clear static verdict. Its rate is 0 / 1.

Headline ``verdict_agreement_rate`` is the live arm's: 1 agreeing of 2 sufficient
pairs, with D excluded, so 0.5. Siblings: A ran one passing and one failing, B
one passing, and D one that never completed, so ``sibling_failure_rate`` is
1 / 3 = 0.3333.
"""

from __future__ import annotations

from trace_harness.runner.batch import BatchRunEntry, BatchSummary, aggregate_entries
from trace_harness.runner.experiment import ConditionSpec, derive_metrics
from trace_harness.runner.repair_effectiveness import build_repair_effectiveness
from trace_harness.runner.suite import AgentConfig
from trace_harness.runner.verdict_agreement import (
    blocking_after_fork,
    recorded_batches,
    score_pairs,
)
from trace_harness.tracing.events import utc_now
from trace_harness.verifiers.base import FailedCheck, VerifierResult

CONTROL = "ctl_x"
GEMINI = "gemini-3.6-flash"
CLAUDE = "claude-sonnet-5"
VERDICTS: dict[str, VerifierResult] = {}


def _check(steps: list[int], blocks: bool = True) -> FailedCheck:
    return FailedCheck(
        check_id="c", message="m", expected="e", actual="a", step_ids=steps, blocks_release=blocks
    )


def _verdict(run_id: str, *checks: FailedCheck, incomplete: bool = False) -> VerifierResult:
    verdict = "incomplete" if incomplete else ("fail" if checks else "pass")
    return VerifierResult(
        verifier_id="refund_policy",
        run_id=run_id,
        passed=verdict == "pass",
        verdict=verdict,
        failed_checks=list(checks),
    )


def _runs(batch: str, task: str, provider: str, model: str, runs: list) -> list[BatchRunEntry]:
    """``runs`` holds the failed checks of each seed, or the status of a run that did not finish."""
    entries = []
    for seed, run in enumerate(runs):
        run_id = None if run == "setup_error" else f"{batch}_{seed}"
        status = run if isinstance(run, str) else "completed"
        if run_id and status == "completed":
            VERDICTS[run_id] = _verdict(run_id, *run)
        elif run_id:
            # A verdict file from before 0.4.0 says fail on a run that never
            # finished. Run status alone keeps it out of every denominator.
            VERDICTS[run_id] = _verdict(run_id, _check([99]))
        entries.append(
            BatchRunEntry(
                run_id=run_id,
                task_id=task,
                task_path=f"fixtures/tasks/{task}.json",
                agent_label=batch,
                provider=provider,
                model=model,
                status=status,
                seed=seed,
            )
        )
    return entries


def _batch(batch_id, kind, source, step, entries, control=CONTROL, **metadata):
    summary = BatchSummary(
        batch_id=batch_id,
        suite_id="s",
        started_at=utc_now(),
        finished_at=utc_now(),
        agent_configs=[AgentConfig(label=batch_id)],
        entries=entries,
        aggregates=aggregate_entries(entries),
        metadata={"source_run_id": source, **metadata},
    )
    condition = ConditionSpec(
        name=batch_id,
        kind=kind,
        agent_config=AgentConfig(label=batch_id),
        control_ids=[control] if control else [],
        start={"source_run_id": source, "step_id": step},
    )
    return summary, condition


def _static(batch_id, source, task, exit_code, siblings):
    entry = _runs(batch_id, task, "fixture", "scripted:x", [[_check([1])]])
    for run_id, verdict in siblings.items():
        VERDICTS[run_id] = verdict
    sibling_rows = [{"test_name": r, "run_id": r} for r in siblings]
    return _batch(
        batch_id,
        "static_replay",
        source,
        1,
        entry,
        replay_exit_code=exit_code,
        siblings=sibling_rows,
    )


BLOCK_AFTER = {5: [_check([6])], 3: [_check([4])], 4: [_check([5])]}


def _experiment():
    VERDICTS.clear()
    after_a = [_check([6])]
    batches = [
        _static(
            "sA",
            "run_A",
            "task_a",
            0,
            {"sib1": _verdict("sib1"), "sib2": _verdict("sib2", _check([2]))},
        ),
        _static("sB", "run_B", "task_b", 1, {"sib3": _verdict("sib3")}),
        _static("sD", "run_D", "task_d", 1, {"sib4": _verdict("sib4", incomplete=True)}),
        # A plain replay installs no control, so its failing sibling is not A4's.
        _batch(
            "sE",
            "static_replay",
            "run_A",
            1,
            [],
            control=None,
            replay_exit_code=1,
            siblings=[{"test_name": "sib5", "run_id": "sib5"}],
        ),
        # A live, fork 5: a pass, a block at the fork step itself, a non-blocking
        # check after it, a blocking check without steps, one blocking at steps
        # 3 and 6, and an incomplete run. 4 clear of 5 completed.
        _batch(
            "lA",
            "live",
            "run_A",
            5,
            _runs(
                "lA",
                "task_a",
                "gemini",
                GEMINI,
                [
                    [],
                    [_check([5])],
                    [_check([7], blocks=False)],
                    [_check([])],
                    [_check([3, 6])],
                    "terminated",
                ],
            ),
        ),
        # A control off: 4 of 5 blocking after the fork.
        _batch(
            "nA",
            "live_no_control",
            "run_A",
            5,
            _runs("nA", "task_a", "gemini", GEMINI, [after_a, after_a, after_a, after_a, []]),
            control=None,
        ),
        # B live, fork 3: 3 clear of 6 completed.
        _batch(
            "lB",
            "live",
            "run_B",
            3,
            _runs(
                "lB",
                "task_b",
                "gemini",
                GEMINI,
                [[], [], [_check([3])], BLOCK_AFTER[3], BLOCK_AFTER[3], BLOCK_AFTER[3]],
            ),
        ),
        # B control off: a block at the fork step only, so nothing after it.
        _batch(
            "nB",
            "live_no_control",
            "run_B",
            3,
            _runs("nB", "task_b", "gemini", GEMINI, [[_check([3])], [], [], [], []]),
            control=None,
        ),
        # D live, fork 4: 4 completed (2 blocking after) and a setup error.
        _batch(
            "lD",
            "live",
            "run_D",
            4,
            _runs(
                "lD",
                "task_d",
                "gemini",
                GEMINI,
                [[], [], BLOCK_AFTER[4], BLOCK_AFTER[4], "setup_error"],
            ),
        ),
        # The swapped model on A blocks after the fork on every seed.
        _batch(
            "wA",
            "live_swapped",
            "run_A",
            5,
            _runs("wA", "task_a", "anthropic", CLAUDE, [after_a] * 5),
        ),
    ]
    VERDICTS["sib5"] = _verdict("sib5", _check([2]))
    summaries = [s for s, _ in batches]
    conditions = {s.batch_id: c for s, c in batches}
    return summaries, conditions


def test_blocking_after_the_fork_counts_only_release_blocking_steps_past_it():
    fail = _verdict
    assert not blocking_after_fork(fail("r"), 5)
    assert not blocking_after_fork(fail("r", _check([5])), 5)
    assert not blocking_after_fork(fail("r", _check([7], blocks=False)), 5)
    assert not blocking_after_fork(fail("r", _check([])), 5)
    assert blocking_after_fork(fail("r", _check([3, 6])), 5)
    assert not blocking_after_fork(fail("r", _check([6]), incomplete=True), 5)


def test_the_hand_computed_rates():
    summaries, conditions = _experiment()
    metrics = derive_metrics(summaries, conditions=conditions, verifier_results=VERDICTS)

    assert metrics.verdict_agreement_rate == 0.5
    assert metrics.sibling_failure_rate == 0.3333
    extra = metrics.extra
    assert (extra["verdict_agreement_k"], extra["verdict_agreement_n"]) == (1, 2)
    assert extra["verdict_agreement_excluded"] == 1
    assert extra[f"verdict_agreement_rate/live/{GEMINI}"] == 0.5
    assert extra[f"verdict_agreement_rate/live_swapped/{CLAUDE}"] == 0.0
    assert (
        extra[f"verdict_agreement_k/live_swapped/{CLAUDE}"],
        extra[f"verdict_agreement_n/live_swapped/{CLAUDE}"],
    ) == (0, 1)
    assert (extra["sibling_failure_k"], extra["sibling_failure_n"]) == (1, 3)
    # The per-seed share beside every majority, as the memo's blind spot asks.
    assert extra[f"pair/live/{GEMINI}/task_a/{CONTROL}/live_clear_share"] == 0.8
    assert extra[f"pair/live/{GEMINI}/task_a/{CONTROL}/completed_seeds"] == 5
    assert extra[f"pair/live/{GEMINI}/task_a/{CONTROL}/static_clear"] == 1.0
    assert extra[f"pair/live/{GEMINI}/task_b/{CONTROL}/live_clear_share"] == 0.5
    assert extra[f"pair/live/{GEMINI}/task_b/{CONTROL}/static_clear"] == 0.0
    assert extra[f"pair/live/{GEMINI}/task_d/{CONTROL}/completed_seeds"] == 4
    assert extra[f"pair/live_swapped/{CLAUDE}/task_a/{CONTROL}/live_clear_share"] == 0.0


def test_the_pairs_state_every_exclusion():
    summaries, conditions = _experiment()
    pairs = score_pairs(recorded_batches(summaries, conditions), VERDICTS)
    table = {
        (p.kind, p.task_id): (
            p.static_clear,
            p.clear_seeds,
            p.completed_seeds,
            p.agrees,
            p.excluded,
        )
        for p in pairs
    }
    assert table == {
        ("live", "task_a"): (True, 4, 5, True, None),
        ("live", "task_b"): (False, 3, 6, False, None),
        ("live", "task_d"): (False, 2, 4, None, "4 completed seed(s), fewer than 5"),
        ("live_swapped", "task_a"): (True, 0, 5, False, None),
    }


def test_b1_by_hand():
    summaries, conditions = _experiment()
    report = build_repair_effectiveness("exp", recorded_batches(summaries, conditions), VERDICTS)
    rows = {
        (e.artifact_id, e.model): (
            (e.control_on.blocking_failures_after_fork, e.control_on.completed_runs),
            (e.control_off.blocking_failures_after_fork, e.control_off.completed_runs),
            e.repair_effectiveness,
            e.null_reason,
        )
        for e in report.entries
    }
    assert rows == {
        ("run_A", GEMINI): ((1, 5), (4, 5), 0.75, None),
        ("run_B", GEMINI): ((3, 6), (0, 5), None, "the baseline nB recorded no blocking failure"),
        ("run_D", GEMINI): ((2, 4), (0, 0), None, "no completed runs under live_no_control"),
        ("run_A", CLAUDE): ((5, 5), (0, 0), None, "no completed runs under live_no_control"),
    }
    by_artifact = {(e.artifact_id, e.model): e for e in report.entries}
    assert by_artifact[("run_A", GEMINI)].fork_step == 5
    assert by_artifact[("run_A", GEMINI)].control_on.condition == "lA"
    assert by_artifact[("run_A", GEMINI)].control_off.batch_id == "nA"


def test_static_replay_never_enters_b1():
    summaries, conditions = _experiment()
    report = build_repair_effectiveness("exp", recorded_batches(summaries, conditions), VERDICTS)
    assert all(e.control_on.condition[0] in "lw" for e in report.entries)


def test_two_models_on_the_live_arm_are_never_pooled():
    summaries, conditions = _experiment()
    extra_run = _batch(
        "fA", "live", "run_A", 5, _runs("fA", "task_a", "fixture", "scripted:x", [[]] * 5)
    )
    summaries.append(extra_run[0])
    conditions[extra_run[0].batch_id] = extra_run[1]
    metrics = derive_metrics(summaries, conditions=conditions, verifier_results=VERDICTS)
    assert metrics.verdict_agreement_rate is None
    assert metrics.extra["verdict_agreement_rate/live/fixture"] == 1.0
    assert metrics.extra[f"verdict_agreement_rate/live/{GEMINI}"] == 0.5


def test_without_verdicts_both_metrics_stay_null():
    summaries, conditions = _experiment()
    metrics = derive_metrics(summaries, conditions=conditions)
    assert metrics.verdict_agreement_rate is None and metrics.sibling_failure_rate is None


def test_a_static_batch_without_an_exit_code_gives_no_static_verdict():
    summaries, conditions = _experiment()
    del summaries[0].metadata["replay_exit_code"]
    pairs = score_pairs(recorded_batches(summaries, conditions), VERDICTS)
    (a_live,) = [p for p in pairs if (p.kind, p.task_id) == ("live", "task_a")]
    assert a_live.excluded == "no static_replay verdict for this artifact and control"
