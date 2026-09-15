"""TRA-90: the per-batch suite report — checks fired, failure categories, coverage.

Runs the real ``refund_v0`` / ``refund_bundles_v0`` manifests through the batch
pipeline into pytest temp dirs (fully offline) and pins the rolled-up report.
The pinned artifact lives at ``fixtures/expected/refund_v0_suite_report.json``;
regenerate it with ``scratchpad``-style scripting only via
``build_suite_report`` (never hand-edit).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from conftest import FIXTURES_DIR
from trace_harness.cli import main
from trace_harness.run_reader import RunReader
from trace_harness.runner.batch import BatchRunner
from trace_harness.runner.report import (
    SUITE_REPORT_SCHEMA_VERSION,
    SuiteReport,
    build_suite_report,
    family_for_task_path,
    render_suite_report_markdown,
)
from trace_harness.runner.suite import load_suite
from trace_harness.tracing import artifact_store as names
from trace_harness.tracing.artifact_store import ArtifactStore

REFUND_V0 = FIXTURES_DIR / "suites" / "refund_v0.json"
REFUND_BUNDLES_V0 = FIXTURES_DIR / "suites" / "refund_bundles_v0.json"
PINNED_REPORT = FIXTURES_DIR / "expected" / "refund_v0_suite_report.json"


def _run(manifest: Path, runs_dir: Path) -> tuple[ArtifactStore, SuiteReport]:
    store = ArtifactStore(runs_dir)
    summary = BatchRunner(store).run(load_suite(manifest))
    return store, build_suite_report(summary, store)


def _pinnable(report: SuiteReport) -> dict:
    """Strip volatile provenance so the report is byte-stable across runs."""
    data = report.model_dump(mode="json")
    data.pop("batch_id", None)
    data.pop("generated_at", None)
    for row in data["rows"]:
        row["run_id"] = None
    return data


# --- the pin -------------------------------------------------------------


def test_refund_v0_report_matches_pinned_expectation(tmp_path: Path) -> None:
    _store, report = _run(REFUND_V0, tmp_path / "runs")

    assert report.schema_version == SUITE_REPORT_SCHEMA_VERSION
    assert report.total_rows == 29
    assert report.failing_rows == 11
    assert sum(1 for r in report.rows if r.verifier_passed is False) == 11

    expected = json.loads(PINNED_REPORT.read_text(encoding="utf-8"))
    assert _pinnable(report) == expected


def test_refund_v0_failure_category_counts_track_the_bundle_doc(tmp_path: Path) -> None:
    """`by_failure_category` must line up with docs/acceptance/failure-bundles-v0.md.

    Bundle #1 (`refund_policy_failure`) is `stale_source_authority`; the two
    authorization-bypass negatives are `unsafe_irreversible_action`; the two
    final-answer negatives are `inconsistent_final_answer`; the missing
    escalation is `clarification_failure`. The five checks with no attributor
    mapping (escalation hygiene + retrieval completeness) land in `unknown`.
    """
    _store, report = _run(REFUND_V0, tmp_path / "runs")

    assert report.totals.by_failure_category == {
        "clarification_failure": 1,
        "inconsistent_final_answer": 2,
        "stale_source_authority": 1,
        "unknown": 5,
        "unsafe_irreversible_action": 2,
    }
    assert sum(report.totals.by_failure_category.values()) == report.failing_rows


def test_check_id_and_family_totals(tmp_path: Path) -> None:
    _store, report = _run(REFUND_V0, tmp_path / "runs")

    # `required_escalation_missing` fires twice: the compound canonical failure
    # and the dedicated escalation negative.
    assert report.totals.by_check_id["required_escalation_missing"] == 2
    assert report.totals.by_check_id["unauthorized_cash_refund"] == 2
    assert sum(report.totals.by_family.values()) == 29
    assert report.totals.by_agent_label == {"fixture-baseline": 29}
    assert report.totals.pass_rate_by_family["escalation"] == 0.0
    assert report.totals.pass_rate_by_family["customer_wording"] == 1.0


# --- coverage ----------------------------------------------------------


def test_claimed_but_never_observed_is_non_empty(tmp_path: Path) -> None:
    """Acceptance criterion #3: the coverage logic actually ran."""
    _store, report = _run(REFUND_V0, tmp_path / "runs")

    gap = report.coverage.claimed_never_observed
    assert gap, "expected some claimed failure modes to have no observed category"
    # Retrieval-completeness modes are claimed but the checks map to `unknown`.
    assert {
        "query_formation_error",
        "grounding_citation_error",
        "retrieval_selection_error",
    } <= set(gap)
    # The grid keys every claimed mode, and the gap list is a subset of it.
    assert set(gap) <= set(report.coverage.claimed_vs_observed)
    # Modes whose only failing tasks produced `unknown` have an empty grid row.
    assert report.coverage.claimed_vs_observed["query_formation_error"] == []
    assert report.coverage.claimed_vs_observed["grounding_citation_error"] == []
    # A gap mode can still have a non-empty grid row (its tasks produced *other*
    # categories) — the mode name just never appears among them.
    assert "policy_violation" not in report.coverage.claimed_vs_observed["policy_violation"]
    # `unknown` is a sentinel, never surfaced as an observed category.
    assert "unknown" not in report.coverage.observed_never_claimed


def test_refund_bundles_v0_differentiates_the_five_failures(tmp_path: Path) -> None:
    """The five bundle rows each carry a real (non-`unknown`) category.

    "Five different categories" refers to five distinct bundles (each
    tripping a different verifier check, per failure-bundles-v0.md), not five
    mutually unique category strings — confirmed with the TPM. The day-31 and
    day-45 authorization bypasses share `unsafe_irreversible_action` by
    design, so the primary-category set has 4 distinct values over the 5
    rows; every row is still categorized (no `unknown`), a real contrast with
    `refund_v0`, where 5 of 11 failing rows are `unknown`. See
    docs/suite_report.md.
    """
    _store, report = _run(REFUND_BUNDLES_V0, tmp_path / "runs")

    failing = [r for r in report.rows if r.verifier_passed is False]
    assert len(failing) == 5
    assert all(r.primary_failure_category != "unknown" for r in failing)
    assert {r.primary_failure_category for r in failing} == {
        "stale_source_authority",
        "unsafe_irreversible_action",
        "clarification_failure",
        "inconsistent_final_answer",
    }


# --- degradation ------------------------------------------------------


def test_missing_attribution_is_unknown_with_a_warning(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "runs")
    summary = BatchRunner(store).run(load_suite(REFUND_V0))

    victim = next(e for e in summary.entries if e.verifier_passed is False)
    store.artifact_path(victim.run_id, names.ATTRIBUTION_RESULT).unlink()

    report = build_suite_report(summary, store)
    row = next(r for r in report.rows if r.task_id == victim.task_id)
    assert row.primary_failure_category == "unknown"
    assert row.contributing_failure_categories == []
    assert any(
        victim.task_id in w and "attribution_result.json is missing" in w for w in report.warnings
    )


def test_setup_error_entry_still_produces_a_row(tmp_path: Path) -> None:
    """A run-less cell (setup failure) is a row with no verdict, never a crash."""
    from trace_harness.runner.batch import BatchRunEntry, BatchSummary
    from trace_harness.tracing.events import utc_now

    store = ArtifactStore(tmp_path / "runs")
    now = utc_now()
    summary = BatchSummary(
        batch_id="batch_test",
        suite_id="synthetic",
        started_at=now,
        finished_at=now,
        agent_configs=[],
        entries=[
            BatchRunEntry(
                run_id=None,
                task_id="broken_task",
                task_path="fixtures/tasks/refund_task_families/purchase_age/x/broken_task.json",
                agent_label="fixture-baseline",
                provider="fixture",
                status="setup_error",
                error="TaskLoadError: boom",
            )
        ],
        aggregates=_zero_aggregates(),
    )
    report = build_suite_report(summary, store)
    assert len(report.rows) == 1
    row = report.rows[0]
    assert row.run_id is None
    assert row.family == "purchase_age"
    assert row.verifier_passed is None
    assert row.primary_failure_category == ""


def _zero_aggregates():
    from trace_harness.runner.batch import BatchAggregates

    return BatchAggregates(
        total=1,
        completed=0,
        terminated=0,
        errored=1,
        verifier_passed=0,
        verifier_failed=0,
        cost_recorded=0,
        known_cost_usd=0.0,
    )


# --- performance -----------------------------------------------------


def test_report_generation_is_well_under_one_second(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "runs")
    summary = BatchRunner(store).run(load_suite(REFUND_V0))

    start = time.perf_counter()
    build_suite_report(summary, store)
    assert time.perf_counter() - start < 1.0


# --- store + CLI ----------------------------------------------------


def test_family_derivation() -> None:
    assert family_for_task_path("fixtures/tasks/refund_policy_failure.json") == "canonical"
    assert (
        family_for_task_path(
            "fixtures/tasks/refund_task_families/purchase_age/day_0/refund_cash_age_boundary_day_0.json"
        )
        == "purchase_age"
    )
    # Windows-style separators resolve identically.
    assert (
        family_for_task_path("fixtures\\tasks\\refund_task_families\\escalation\\x\\y.json")
        == "escalation"
    )


def test_run_suite_report_flag_writes_json_and_markdown(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    rc = main(["--runs-dir", str(runs_dir), "run-suite", str(REFUND_V0), "--report"])
    assert rc == 0

    store = ArtifactStore(runs_dir)
    batch_id = _only_batch_id(runs_dir)
    assert store.suite_report_path(batch_id).is_file()
    assert store.suite_report_md_path(batch_id).is_file()

    report = SuiteReport.model_validate(store.read_suite_report(batch_id))
    assert report.total_rows == 29
    md = store.suite_report_md_path(batch_id).read_text(encoding="utf-8")
    assert md.startswith("# Suite report — refund_v0")
    assert "## Coverage" in md


def test_report_suite_subcommand_then_run_reader_round_trip(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    assert main(["--runs-dir", str(runs_dir), "run-suite", str(REFUND_V0)]) == 0
    batch_id = _only_batch_id(runs_dir)

    assert main(["--runs-dir", str(runs_dir), "report-suite", batch_id]) == 0

    report = RunReader(ArtifactStore(runs_dir)).get_suite_report(batch_id)
    assert report.batch_id == batch_id
    assert report.failing_rows == 11
    assert isinstance(render_suite_report_markdown(report), str)


def test_report_suite_unknown_batch_is_a_clean_input_error(tmp_path: Path, capsys) -> None:
    rc = main(["--runs-dir", str(tmp_path / "runs"), "report-suite", "batch_does_not_exist"])
    assert rc == 2
    assert "batch summary not found" in capsys.readouterr().err


def test_get_suite_report_builds_on_the_fly_when_absent(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    store = ArtifactStore(runs_dir)
    summary = BatchRunner(store).run(load_suite(REFUND_V0))

    # No report written yet — RunReader falls back to building it in memory.
    report = RunReader(store).get_suite_report(summary.batch_id)
    assert report.total_rows == 29
    assert not store.suite_report_path(summary.batch_id).is_file()


def _only_batch_id(runs_dir: Path) -> str:
    batches = sorted((runs_dir / names.BATCHES_DIR).iterdir())
    assert len(batches) == 1
    return batches[0].name
