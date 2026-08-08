"""Batch-suite execution: cross-product runs, failure isolation, and summary.

Uses the real repo fixtures (no synthetic tasks) and writes into pytest temp
dirs. Fully offline — the fixture provider needs no keys or network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import FAILURE_TASK_PATH, FIXTURES_DIR, VALID_TASK_PATH
from trace_harness.models.base import ActionKind, AgentAction
from trace_harness.run_reader import RunReader
from trace_harness.runner.batch import BatchRunner, BatchSummary, summary_path
from trace_harness.runner.pipeline import run_task_pipeline
from trace_harness.runner.suite import AgentConfig, SuiteLoadError, SuiteSpec, load_suite
from trace_harness.tracing.artifact_store import ArtifactStore

AMBIGUOUS_TASK_PATH = FIXTURES_DIR / "tasks" / "counterexamples" / "refund_policy_ambiguous.json"
SUITE_MANIFEST = FIXTURES_DIR / "suites" / "refund_v0.json"


def _fixture_config(label: str = "fixture-baseline") -> AgentConfig:
    return AgentConfig(label=label, provider="fixture")


# --- suite loading / validation ---


def _suite_task_paths_by_id() -> dict[str, Path]:
    """task_id -> absolute fixture path, for every task in the canonical manifest."""
    from trace_harness.tasks.loader import load_task

    out: dict[str, Path] = {}
    for rel in load_suite(SUITE_MANIFEST).tasks:
        p = FIXTURES_DIR.parent / rel
        out[load_task(p).task_id] = p
    return out


# Every negative in the suite pins its exact verifier outcome under fixtures/expected/.
_PINNED_EXPECTED = sorted((FIXTURES_DIR / "expected").glob("*_expected_verifier.json"))


@pytest.mark.parametrize("expected_path", _PINNED_EXPECTED, ids=lambda p: p.stem)
def test_pinned_negative_matches_expectation(expected_path: Path, tmp_path: Path) -> None:
    """Each pinned expected-verifier file must match a real run of its task: same
    pass/fail, same failed-check set, same severity and blocks_release. This is the
    exact-outcome contract for every suite negative (deliverable #2)."""
    from conftest import run_task_fixture
    from trace_harness.verifiers.base import VerifierInput
    from trace_harness.verifiers.registry import get_verifier

    doc = json.loads(expected_path.read_text())
    task_id, expected = doc["task_id"], doc["expected"]
    task_path = _suite_task_paths_by_id().get(task_id)
    if task_path is None:
        pytest.skip(f"{task_id} is pinned but not in the canonical suite manifest")

    run = run_task_fixture(task_path, tmp_path / "runs")
    result = get_verifier(run.task.verifier_ids[0]).verify(
        VerifierInput.from_parts(
            task=run.task, trace=run.trace, final_state=run.final_state, run_id=run.run_id
        )
    )
    assert result.passed is expected["passed"]
    assert sorted(c.check_id for c in result.failed_checks) == sorted(expected["failed_check_ids"])
    assert result.severity.value == expected["severity"]
    assert result.blocks_release is expected["blocks_release"]


def test_every_failing_suite_task_has_a_pinned_expectation(tmp_path: Path) -> None:
    """
    Covers the inverse of test_pinned_negative_matches_expectation: run the suite,
    take the task_ids that actually failed verification, and assert every one of 
    them has a matching pinned file.
    """
    summary = BatchRunner(ArtifactStore(tmp_path / "runs")).run(load_suite(SUITE_MANIFEST))

    failing_task_ids = {entry.task_id for entry in summary.entries if not entry.verifier_passed}
    pinned_task_ids = {json.loads(p.read_text())["task_id"] for p in _PINNED_EXPECTED}

    missing_pins = failing_task_ids - pinned_task_ids
    assert not missing_pins, (
        "suite task(s) fail verification but have no pinned "
        f"fixtures/expected/*_expected_verifier.json file: {sorted(missing_pins)}"
    )


def test_every_canonical_suite_task_passes_authoring_validation() -> None:
    """Acceptance rule: every task in the runnable suite must pass the authoring
    rubric. The validation CLI only globs top-level fixtures/tasks/, so nested
    family tasks would otherwise go unvalidated — this ties validation to the
    manifest so any task added to the suite is checked."""
    from trace_harness.tasks.loader import load_task
    from trace_harness.tasks.validation import errors, validate_task

    suite = load_suite(SUITE_MANIFEST)
    for rel in suite.tasks:
        task = load_task(FIXTURES_DIR.parent / rel)
        errs = [i.code for i in errors(validate_task(task))]
        assert errs == [], f"{rel}: {errs}"


def test_load_canonical_suite() -> None:
    suite = load_suite(SUITE_MANIFEST)
    assert suite.suite_id == "refund_v0"
    assert suite.tasks == [
        "fixtures/tasks/refund_policy_failure.json",
        "fixtures/tasks/refund_policy_valid_cash.json",
        "fixtures/tasks/refund_policy_store_credit.json",
        "fixtures/tasks/refund_policy_no_refund.json",
        "fixtures/tasks/refund_policy_missing_info.json",
        "fixtures/tasks/refund_task_families/purchase_age/day_0/refund_cash_age_boundary_day_0.json",
        "fixtures/tasks/refund_task_families/purchase_age/day_30/refund_cash_age_boundary_day_30.json",
        "fixtures/tasks/refund_task_families/purchase_age/day_31_no_approval/refund_cash_age_boundary_day_31_no_approval.json",
        "fixtures/tasks/refund_task_families/purchase_age/day_31_approved/refund_cash_age_boundary_day_31_approved.json",
        "fixtures/tasks/refund_task_families/purchase_age/day_60_approved/refund_cash_age_boundary_day_60_approved.json",
        "fixtures/tasks/refund_task_families/purchase_age/day_61_approved/refund_cash_age_boundary_day_61_approved.json",
        "fixtures/tasks/refund_task_families/purchase_age/day_61_violation/refund_cash_age_boundary_day_61_violation.json",
        "fixtures/tasks/refund_task_families/outage_evidence/day_45_documented/refund_outage_evidence_day_45_documented.json",
        "fixtures/tasks/refund_task_families/outage_evidence/day_45_not_documented/refund_outage_evidence_day_45_not_documented.json",
        "fixtures/tasks/refund_task_families/outage_evidence/day_45_credit_violation/refund_outage_evidence_day_45_credit_violation.json",
        "fixtures/tasks/refund_task_families/escalation/escalation_missing/refund_escalation_missing.json",
        "fixtures/tasks/refund_task_families/escalation/escalation_unnecessary/refund_escalation_unnecessary.json",
        "fixtures/tasks/refund_task_families/escalation/escalation_duplicate/refund_escalation_duplicate.json",
        "fixtures/tasks/refund_task_families/final_answer_consistency/phantom_claim/refund_final_answer_phantom.json",
        "fixtures/tasks/refund_task_families/final_answer_consistency/denied_real_refund/refund_final_answer_denied_real.json",
        "fixtures/tasks/refund_task_families/customer_wording/eligible_neutral/refund_wording_eligible_neutral.json",
        "fixtures/tasks/refund_task_families/customer_wording/eligible_pressure/refund_wording_eligible_pressure.json",
        "fixtures/tasks/refund_task_families/policy_ordering_status/deprecated_ranked_first/refund_policy_order_deprecated_first.json",
        "fixtures/tasks/refund_task_families/refund_type/store_credit_in_window/refund_type_store_credit_in_window.json",
        "fixtures/tasks/refund_task_families/refund_type/cash_with_outage_in_window/refund_type_cash_with_outage_in_window.json",
    ]
    assert suite.agent_configs[0].provider == "fixture"


def test_load_suite_missing_file(tmp_path: Path) -> None:
    with pytest.raises(SuiteLoadError):
        load_suite(tmp_path / "nope.json")


def test_load_suite_bad_json(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    with pytest.raises(SuiteLoadError):
        load_suite(bad)


def test_duplicate_agent_labels_rejected() -> None:
    with pytest.raises(ValueError):
        SuiteSpec(
            suite_id="x",
            tasks=["t.json"],
            agent_configs=[AgentConfig(label="a"), AgentConfig(label="a")],
        )


# --- batch execution ---


def test_canonical_suite_executes_all_product_outcomes(tmp_path: Path) -> None:
    summary = BatchRunner(ArtifactStore(tmp_path / "runs")).run(load_suite(SUITE_MANIFEST))

    assert {entry.task_id: entry.verifier_passed for entry in summary.entries} == {
        # Five canonical outcomes
        "refund_policy_failure": False,
        "refund_policy_valid_cash": True,
        "refund_policy_store_credit": True,
        "refund_policy_no_refund": True,
        "refund_policy_missing_info": True,
        # purchase_age family: cash-window boundary controls (all correct behavior)
        "refund_cash_age_boundary_day_0": True,
        "refund_cash_age_boundary_day_30": True,
        "refund_cash_age_boundary_day_31_no_approval": True,
        "refund_cash_age_boundary_day_31_approved": True,
        "refund_cash_age_boundary_day_60_approved": True,
        "refund_cash_age_boundary_day_61_approved": True,
        # outage_evidence family: documented↔not_documented controls + enforcement negative
        "refund_outage_evidence_day_45_documented": True,
        "refund_outage_evidence_day_45_not_documented": True,
        "refund_outage_evidence_day_45_credit_violation": False,
        # escalation family negatives (slice 3)
        "refund_escalation_missing": False,
        "refund_escalation_unnecessary": False,
        "refund_escalation_duplicate": False,
        # final_answer_consistency family + >60 cash-boundary negatives (slice 4)
        "refund_final_answer_phantom": False,
        "refund_final_answer_denied_real": False,
        "refund_cash_age_boundary_day_61_violation": False,
        # customer_wording family (tone invariance)
        "refund_wording_eligible_neutral": True,
        "refund_wording_eligible_pressure": True,
        # policy_ordering_status family (retrieval-order robustness)
        "refund_policy_order_deprecated_first": True,
        # refund_type family (remedy choice)
        "refund_type_store_credit_in_window": True,
        "refund_type_cash_with_outage_in_window": True,
    }
    assert summary.aggregates.total == 25
    assert summary.aggregates.completed == 25
    assert summary.aggregates.terminated == 0
    assert summary.aggregates.errored == 0
    assert summary.aggregates.verifier_passed == 17
    assert summary.aggregates.verifier_failed == 8


def test_batch_runs_all_and_aggregates(tmp_path: Path) -> None:
    suite = SuiteSpec(
        suite_id="two",
        tasks=[str(FAILURE_TASK_PATH), str(VALID_TASK_PATH)],
        agent_configs=[_fixture_config()],
    )
    store = ArtifactStore(tmp_path / "runs")
    summary = BatchRunner(store).run(suite)

    assert len(summary.entries) == 2
    agg = summary.aggregates
    assert agg.total == 2
    assert agg.completed == 2
    assert agg.terminated == 0
    assert agg.errored == 0
    assert agg.verifier_passed == 1
    assert agg.verifier_failed == 1
    assert agg.pass_rate == 0.5
    assert agg.cost_recorded == 2
    assert agg.known_cost_usd == 0.0
    for e in summary.entries:
        assert e.run_id is not None
        assert store.run_dir(e.run_id).is_dir()
        assert e.latency_ms is not None
        assert e.cost_usd == 0.0

    listed = {run.task_id: run.verifier_passed for run in RunReader(store).list_runs()}
    assert listed == {
        "refund_policy_failure": False,
        "refund_policy_valid_cash": True,
    }


def test_batch_tags_run_index_entries_with_batch_id(tmp_path: Path) -> None:
    """Every completed cell's index entry carries the batch's id, and the
    RunReader batch filter returns exactly those runs."""
    suite = SuiteSpec(
        suite_id="tagged",
        tasks=[str(FAILURE_TASK_PATH), str(VALID_TASK_PATH)],
        agent_configs=[_fixture_config()],
    )
    store = ArtifactStore(tmp_path / "runs")
    summary = BatchRunner(store).run(suite)

    reader = RunReader(store)
    summaries = reader.list_runs()
    assert {s.batch_id for s in summaries} == {summary.batch_id}

    filtered = reader.list_runs_for_batch(summary.batch_id)
    assert {s.run_id for s in filtered} == {s.run_id for s in summaries}
    assert reader.list_runs_for_batch("batch_absent") == []

    # The index is derived state: deleting/rebuilding it must preserve batch
    # membership from the authoritative batch summary.
    store.index_path().unlink()
    rebuilt = store.rebuild_index()
    assert {entry.batch_id for entry in rebuilt.entries} == {summary.batch_id}


def test_batch_counts_terminated_runs_separately(tmp_path: Path) -> None:
    suite = SuiteSpec(
        suite_id="terminated",
        tasks=[str(VALID_TASK_PATH)],
        agent_configs=[AgentConfig(label="one-step", provider="fixture", max_steps=1)],
    )

    summary = BatchRunner(ArtifactStore(tmp_path / "runs")).run(suite)

    assert summary.entries[0].status == "terminated"
    assert summary.aggregates.total == 1
    assert summary.aggregates.completed == 0
    assert summary.aggregates.terminated == 1
    assert summary.aggregates.errored == 0
    assert summary.aggregates.pass_rate is None
    assert summary.aggregates.by_agent["one-step"]["terminated"] == 1


def test_batch_isolates_setup_failure(tmp_path: Path) -> None:
    # The ambiguous counterexample has no fixture_script -> a setup failure that
    # must NOT crash the batch or lose the other runs.
    suite = SuiteSpec(
        suite_id="isolation",
        tasks=[str(FAILURE_TASK_PATH), str(AMBIGUOUS_TASK_PATH), str(VALID_TASK_PATH)],
        agent_configs=[_fixture_config()],
    )
    store = ArtifactStore(tmp_path / "runs")
    summary = BatchRunner(store).run(suite)

    assert len(summary.entries) == 3  # batch completed all cells
    errored = [e for e in summary.entries if e.status == "setup_error"]
    assert len(errored) == 1
    assert errored[0].run_id is None
    assert errored[0].error
    # the two runnable tasks still ran and were verified
    assert summary.aggregates.completed == 2
    assert summary.aggregates.errored == 1
    assert summary.aggregates.verifier_passed == 1
    assert summary.aggregates.verifier_failed == 1


def test_summary_written_and_reparses(tmp_path: Path) -> None:
    suite = SuiteSpec(suite_id="w", tasks=[str(VALID_TASK_PATH)], agent_configs=[_fixture_config()])
    store = ArtifactStore(tmp_path / "runs")
    summary = BatchRunner(store).run(suite)

    path = summary_path(store.runs_dir, summary.batch_id)
    assert path.is_file()
    reloaded = BatchSummary.model_validate(json.loads(path.read_text(encoding="utf-8")))
    assert reloaded.batch_id == summary.batch_id
    assert reloaded.suite_id == "w"
    assert len(reloaded.entries) == 1


def test_summary_is_durable_before_index_enrichment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suite = SuiteSpec(suite_id="ordered", tasks=[str(VALID_TASK_PATH)])
    store = ArtifactStore(tmp_path / "runs")
    original = store.enrich_index_entry_with_batch

    def assert_summary_exists(run_id: str, batch_id: str) -> None:
        assert store.batch_summary_path(batch_id).is_file()
        original(run_id, batch_id)

    monkeypatch.setattr(store, "enrich_index_entry_with_batch", assert_summary_exists)
    BatchRunner(store).run(suite)


def test_agent_config_metadata_recorded(tmp_path: Path) -> None:
    suite = SuiteSpec(
        suite_id="cfg",
        tasks=[str(VALID_TASK_PATH)],
        agent_configs=[AgentConfig(label="fixture-baseline", provider="fixture")],
    )
    store = ArtifactStore(tmp_path / "runs")
    entry = BatchRunner(store).run(suite).entries[0]
    assert entry.agent_label == "fixture-baseline"
    assert entry.provider == "fixture"
    assert entry.prompt_version  # recorded for reproducibility
    assert entry.task_schema_version  # task "version" recorded
    assert entry.model and entry.model.startswith("scripted:")


def test_multiple_agent_configs_cross_product(tmp_path: Path) -> None:
    suite = SuiteSpec(
        suite_id="cross",
        tasks=[str(VALID_TASK_PATH)],
        agent_configs=[_fixture_config("a"), _fixture_config("b")],
    )
    store = ArtifactStore(tmp_path / "runs")
    summary = BatchRunner(store).run(suite)

    assert len(summary.entries) == 2  # 1 task x 2 configs
    assert {e.agent_label for e in summary.entries} == {"a", "b"}
    assert set(summary.aggregates.by_agent.keys()) == {"a", "b"}


def test_live_adapter_receives_recorded_agent_knobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    class _FinalAnswerAdapter:
        name = "gemini"

        def next_action(self, transcript, tools):
            return AgentAction(kind=ActionKind.FINAL_ANSWER, final_answer="done")

    def fake_create_model_adapter(provider: str, **kwargs):
        captured.update({"provider": provider, **kwargs})
        return _FinalAnswerAdapter()

    monkeypatch.setattr(
        "trace_harness.runner.pipeline.create_model_adapter",
        fake_create_model_adapter,
    )
    config = AgentConfig(
        label="live",
        provider="gemini",
        model="gemini-3.6-flash",
        temperature=0.2,
        seed=7,
        timeout_seconds=17.0,
    )

    result = run_task_pipeline(
        VALID_TASK_PATH,
        config,
        ArtifactStore(tmp_path / "runs"),
        bundle_on_fail=False,
    )

    assert captured == {
        "provider": "gemini",
        "model": "gemini-3.6-flash",
        "temperature": 0.2,
        "seed": 7,
        "timeout_seconds": 17.0,
    }
    assert result.run_config.temperature == 0.2
    assert result.run_config.seed == 7
    assert result.run_config.timeout_seconds == 17.0
