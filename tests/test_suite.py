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


def test_load_canonical_suite() -> None:
    suite = load_suite(SUITE_MANIFEST)
    assert suite.suite_id == "refund_v0"
    assert suite.tasks == [
        "fixtures/tasks/refund_policy_failure.json",
        "fixtures/tasks/refund_policy_valid_cash.json",
        "fixtures/tasks/refund_policy_store_credit.json",
        "fixtures/tasks/refund_policy_no_refund.json",
        "fixtures/tasks/refund_policy_missing_info.json",
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


def test_canonical_suite_executes_all_five_product_outcomes(tmp_path: Path) -> None:
    summary = BatchRunner(ArtifactStore(tmp_path / "runs")).run(load_suite(SUITE_MANIFEST))

    assert {entry.task_id: entry.verifier_passed for entry in summary.entries} == {
        "refund_policy_failure": False,
        "refund_policy_valid_cash": True,
        "refund_policy_store_credit": True,
        "refund_policy_no_refund": True,
        "refund_policy_missing_info": True,
    }
    assert summary.aggregates.total == 5
    assert summary.aggregates.completed == 5
    assert summary.aggregates.terminated == 0
    assert summary.aggregates.errored == 0
    assert summary.aggregates.verifier_passed == 4
    assert summary.aggregates.verifier_failed == 1


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
