"""The sweep summary (#198): counting over seeds, cost per failure, and labels.

Pure roll-up tests over hand-built batches, so every count is known in advance.
The end-to-end sweep with recorded cassettes is tests/test_sweep.py.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from conftest import REPO_ROOT
from trace_harness.attribution.heuristic import check_category
from trace_harness.attribution.schemas import FailureCategory
from trace_harness.runner.batch import (
    BatchBudget,
    BatchRunEntry,
    BatchSummary,
    NotRunCell,
    aggregate_entries,
)
from trace_harness.runner.suite import AgentConfig, load_suite
from trace_harness.runner.sweep_summary import (
    NATURAL,
    STAGED_TRAP,
    ProviderBatch,
    SweepSummary,
    TaskStaging,
    label_failure,
    load_staging,
    summarize_sweep,
)
from trace_harness.tasks.loader import load_task
from trace_harness.verifiers.base import FailedCheck

A = "fixtures/tasks/refund_policy_failure.json"
B = "fixtures/tasks/refund_policy_valid_cash.json"
C = (
    "fixtures/tasks/refund_task_families/purchase_age/day_61_violation/"
    "refund_cash_age_boundary_day_61_violation.json"
)
D = (
    "fixtures/tasks/refund_task_families/escalation/escalation_duplicate/"
    "refund_escalation_duplicate.json"
)
NOW = datetime(2026, 9, 23, tzinfo=UTC)


@pytest.fixture(autouse=True)
def at_repo_root(monkeypatch: pytest.MonkeyPatch) -> None:
    # Suite task paths and pinned expectations are repository paths.
    monkeypatch.chdir(REPO_ROOT)


def test_refund_v0_stages_exactly_its_pinned_negatives() -> None:
    suite = load_suite("fixtures/suites/refund_v0.json")
    staging = load_staging(suite.tasks)
    negatives = sorted(s.task_id for s in staging.values() if s.staged_negative)
    pinned = sorted(p.name.removesuffix("_expected_verifier.json") for p in _pins())
    assert negatives == pinned
    assert len(negatives) == 14
    tasks = [load_task(path) for path in suite.tasks]
    namers = sorted(t.task_id for t in tasks if t.metadata.get("positive_sibling_tasks"))
    assert namers == negatives
    siblings = sorted(s.task_id for s in staging.values() if s.positive_sibling)
    assert siblings == [
        "refund_cash_age_boundary_day_60_approved",
        "refund_outage_evidence_day_45_documented",
        "refund_policy_missing_info",
        "refund_policy_no_refund",
        "refund_policy_valid_cash",
        "refund_retrieval_full",
        "refund_type_cash_with_outage_in_window",
    ]


def _pins():
    return (REPO_ROOT / "fixtures/expected").glob("*_expected_verifier.json")


@pytest.mark.parametrize(
    ("task", "checks", "label", "natural"),
    [
        # The check the author pinned.
        (C, ["unauthorized_cash_refund"], STAGED_TRAP, []),
        # Unpinned, but filed under unsafe_irreversible_action, the category of
        # the unauthorized_cash_refund that C pins.
        (C, ["unauthorized_store_credit"], STAGED_TRAP, []),
        # Filed under inconsistent_final_answer, a category C pins no check of.
        (
            C,
            ["final_answer_inconsistent_with_state"],
            NATURAL,
            ["final_answer_inconsistent_with_state"],
        ),
        (
            C,
            ["unauthorized_cash_refund", "final_answer_inconsistent_with_state"],
            NATURAL,
            ["final_answer_inconsistent_with_state"],
        ),
        (A, ["required_escalation_missing", "unauthorized_cash_refund"], STAGED_TRAP, []),
        # A valid task and positive sibling stages nothing.
        (B, ["expected_refund_missing"], NATURAL, ["expected_refund_missing"]),
        # D pins duplicate_escalation, which the attributor leaves uncategorized,
        # so only that check is its trap. Its targeted modes list
        # clarification_failure, and a missing escalation there is still natural,
        # as is the forbidden cash refund.
        (D, ["duplicate_escalation"], STAGED_TRAP, []),
        (D, ["required_escalation_missing"], NATURAL, ["required_escalation_missing"]),
        (D, ["unauthorized_cash_refund"], NATURAL, ["unauthorized_cash_refund"]),
    ],
)
def test_labels(task: str, checks: list[str], label: str, natural: list[str]) -> None:
    staging = load_staging([A, B, C, D])
    assert label_failure(staging[task], checks)[:2] == (label, natural)


def test_a_task_stages_only_the_categories_of_its_pinned_checks() -> None:
    staging = load_staging([C, D])
    assert staging[C].pinned_categories == {FailureCategory.UNSAFE_IRREVERSIBLE_ACTION}
    assert staging[D].pinned_categories == frozenset()
    assert check_category("unauthorized_store_credit") is (
        FailureCategory.UNSAFE_IRREVERSIBLE_ACTION
    )
    assert check_category("required_escalation_missing") is FailureCategory.CLARIFICATION_FAILURE
    assert check_category("duplicate_escalation") is None


def test_a_pinned_task_that_is_also_a_positive_sibling_stages_nothing() -> None:
    staging = TaskStaging(
        task_id="t",
        pinned_checks=frozenset({"unauthorized_cash_refund"}),
        positive_sibling=True,
    )
    assert label_failure(staging, ["unauthorized_cash_refund"])[0] == NATURAL


def _entry(task: str, seed: int, verdict: str | None, run_id: str, **extra) -> BatchRunEntry:
    fields = {
        "run_id": run_id,
        "task_id": _TASK_IDS[task],
        "task_path": task,
        "agent_label": f"x-seed{seed}",
        "provider": "gemini",
        "model": "gemini-3.6-flash",
        "prompt_version": "v0",
        "status": "completed",
        "verdict": verdict,
        "verifier_passed": verdict == "pass",
        "cost_usd": 0.01,
        "seed": seed,
    }
    return BatchRunEntry(**{**fields, **extra})


_TASK_IDS = {
    A: "refund_policy_failure",
    B: "refund_policy_valid_cash",
    C: "refund_cash_age_boundary_day_61_violation",
}


def _check(check_id: str, blocks: bool = True) -> FailedCheck:
    return FailedCheck(
        check_id=check_id, message="m", expected="e", actual="a", blocks_release=blocks
    )


def _batch(batch_id: str, entries: list[BatchRunEntry], not_run=()) -> BatchSummary:
    return BatchSummary(
        batch_id=batch_id,
        suite_id="probe",
        started_at=NOW,
        finished_at=NOW,
        agent_configs=[],
        entries=entries,
        aggregates=aggregate_entries(entries),
        budget=BatchBudget(max_cost_usd=1.0, spent_usd=0.0, not_run=list(not_run)),
    )


GEMINI = AgentConfig(label="gemini-flash", provider="gemini", model="gemini-3.6-flash")
OPENAI = AgentConfig(label="gpt-mini", provider="openai", model="gpt-5-mini")
FAILED = {
    "g-a1": [_check("unauthorized_cash_refund")],
    # A non-blocking check alone: a failing cell, and no verified failure.
    "g-a2": [_check("deprecated_policy_treated_as_authoritative", blocks=False)],
    "g-c1": [_check("unauthorized_store_credit")],
    "g-c3": [_check("final_answer_inconsistent_with_state")],
    "o-b2": [_check("expected_refund_missing"), _check("final_answer_inconsistent_with_state")],
    "o-c1": [_check("unauthorized_cash_refund")],
    "o-c2": [_check("unauthorized_cash_refund")],
}


def _summary(**overrides) -> SweepSummary:
    gemini = [
        _entry(A, 1, "fail", "g-a1"),
        _entry(A, 2, "fail", "g-a2"),
        _entry(A, 3, "pass", "g-a3"),
        *(_entry(B, seed, "pass", f"g-b{seed}") for seed in (1, 2, 3)),
        _entry(C, 1, "fail", "g-c1"),
        _entry(C, 2, "pass", "g-c2"),
        _entry(C, 3, "fail", "g-c3"),
    ]
    openai = [
        _entry(A, 1, "pass", "o-a1"),
        _entry(A, 2, "pass", "o-a2"),
        _entry(A, 3, "incomplete", "o-a3", status="terminated"),
        _entry(B, 1, "pass", "o-b1"),
        _entry(B, 2, "fail", "o-b2"),
        _entry(B, 3, "pass", "o-b3"),
        _entry(C, 1, "fail", "o-c1"),
        _entry(C, 2, "fail", "o-c2"),
    ]
    for entry in openai:
        entry.provider, entry.model = "openai", "gpt-5-mini"
    fields = {
        "sweep_id": "sweep_x",
        "sweep_name": "probe",
        "spec_path": None,
        "suite_id": "probe",
        "task_paths": [A, B, C],
        "seeds": [1, 2, 3],
        "batches": [
            ProviderBatch(GEMINI, _batch("batch_g", gemini)),
            ProviderBatch(
                OPENAI,
                _batch(
                    "batch_o",
                    openai,
                    [NotRunCell(agent_label="gpt-mini-seed3", task_path=C, seed=3)],
                ),
            ),
        ],
        "staging": load_staging([A, B, C]),
        "failed_checks": FAILED,
        "budget": None,
        "started_at": NOW,
        "finished_at": NOW,
    }
    return summarize_sweep(**{**fields, **overrides})


def test_pass_counts_and_flips_per_provider_and_task() -> None:
    summary = _summary()
    rows = {(r.provider_label, r.task_id): r for r in summary.tasks}
    counts = {
        key: (r.passed, r.failed, r.incomplete, r.not_run, r.flipped) for key, r in rows.items()
    }
    assert counts == {
        ("gemini-flash", "refund_policy_failure"): (1, 2, 0, 0, True),
        ("gemini-flash", "refund_policy_valid_cash"): (3, 0, 0, 0, False),
        ("gemini-flash", "refund_cash_age_boundary_day_61_violation"): (1, 2, 0, 0, True),
        # An incomplete seed never makes a flip.
        ("gpt-mini", "refund_policy_failure"): (2, 0, 1, 0, False),
        ("gpt-mini", "refund_policy_valid_cash"): (2, 1, 0, 0, True),
        ("gpt-mini", "refund_cash_age_boundary_day_61_violation"): (0, 2, 0, 1, False),
    }
    assert [p.flipped_tasks for p in summary.providers] == [2, 1]
    assert summary.flipped_tasks == 3
    assert [(p.passed, p.failed, p.incomplete, p.not_run) for p in summary.providers] == [
        (5, 4, 0, 0),
        (4, 3, 1, 1),
    ]


def test_failing_cells_are_labeled() -> None:
    cells = {cell.run_id: cell for cell in _summary().failing_cells}
    labels = {run_id: (cell.label, cell.blocking) for run_id, cell in cells.items()}
    assert labels == {
        "g-a1": (STAGED_TRAP, True),
        "g-a2": (STAGED_TRAP, False),
        "g-c1": (STAGED_TRAP, True),
        "g-c3": (NATURAL, True),
        "o-b2": (NATURAL, True),
        "o-c1": (STAGED_TRAP, True),
        "o-c2": (STAGED_TRAP, True),
    }
    assert cells["o-b2"].natural_check_ids == [
        "expected_refund_missing",
        "final_answer_inconsistent_with_state",
    ]
    assert cells["g-c1"].cassette_path == (
        "cassettes/refund_cash_age_boundary_day_61_violation/gemini-3.6-flash/1.jsonl"
    )


def test_cost_per_verified_failure() -> None:
    summary = _summary()
    assert summary.runs == 17
    assert summary.cost_usd == pytest.approx(0.17)
    # Seven failing cells, one of them non-blocking.
    assert summary.verified_failures == 6
    assert summary.natural_verified_failures == 2
    assert summary.cost_per_verified_failure == pytest.approx(0.17 / 6, abs=1e-6)
    assert summary.cost_per_natural_verified_failure == pytest.approx(0.085)
    assert [p.verified_failures for p in summary.providers] == [3, 3]


def test_an_unknown_cost_is_never_divided_as_zero() -> None:
    gemini = _batch(
        "batch_g",
        [_entry(A, 1, "fail", "g-a1", cost_usd=None), _entry(A, 2, "pass", "g-a2")],
    )
    unknown = _summary(batches=[ProviderBatch(GEMINI, gemini)])
    assert unknown.cost_recorded == 1
    assert unknown.cost_usd == pytest.approx(0.01)
    assert unknown.verified_failures == 1
    assert unknown.cost_per_verified_failure is None


def test_a_started_setup_error_without_a_cost_leaves_cost_per_failure_unknown() -> None:
    """A pipeline that raised after its run began keeps the run id; its cost must be known."""
    gemini = _batch(
        "batch_g",
        [
            _entry(A, 1, "fail", "g-a1"),
            _entry(A, 2, None, "g-a2", status="setup_error", cost_usd=None),
        ],
    )
    summary = _summary(batches=[ProviderBatch(GEMINI, gemini)])
    assert summary.verified_failures == 1
    assert summary.cost_recorded == 1
    assert summary.cost_per_verified_failure is None
    assert summary.cost_per_natural_verified_failure is None


def test_a_setup_error_before_any_run_needs_no_cost() -> None:
    """With no run id the cell never started a run, so it called no provider."""
    gemini = _batch(
        "batch_g",
        [
            _entry(A, 1, "fail", "g-a1"),
            _entry(A, 2, None, None, status="setup_error", cost_usd=None),
        ],
    )
    summary = _summary(batches=[ProviderBatch(GEMINI, gemini)])
    assert summary.cost_recorded == 1
    assert summary.cost_per_verified_failure == pytest.approx(0.01)


def test_only_a_completed_run_passes_or_fails() -> None:
    """A run that ended any other way counts as incomplete, whatever its verdict."""
    gemini = _batch(
        "batch_g",
        [
            _entry(A, 1, "fail", "g-a1", status="terminated"),
            _entry(A, 2, "pass", "g-a2", status="error"),
            _entry(A, 3, "pass", "g-a3"),
        ],
    )
    summary = _summary(batches=[ProviderBatch(GEMINI, gemini)], failed_checks={})
    row = next(r for r in summary.tasks if r.task_path == A)
    assert (row.passed, row.failed, row.incomplete, row.flipped) == (1, 0, 2, False)
    assert summary.failing_cells == []
    assert summary.verified_failures == 0


def test_nothing_failing_has_no_cost_per_failure() -> None:
    passing = _batch("batch_g", [_entry(B, 1, "pass", "g-b1")])
    summary = _summary(batches=[ProviderBatch(GEMINI, passing)])
    assert summary.verified_failures == 0
    assert summary.cost_per_verified_failure is None


def test_the_summary_round_trips() -> None:
    summary = _summary()
    assert SweepSummary.model_validate_json(summary.model_dump_json()) == summary
