"""The batch budget guard (#196): a spend cap that stops a batch, and what it records.

Offline. The live provider is a stub at the ``create_model_adapter`` seam that
answers the way the Anthropic adapter does, with a ``raw`` carrying usage, so
every run has a known price without an SDK, a key, or a network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from conftest import (
    FAILURE_TASK_PATH,
    FIXTURES_DIR,
    NO_REFUND_TASK_PATH,
    REPO_ROOT,
    VALID_TASK_PATH,
)
from trace_harness.cli import main
from trace_harness.models.anthropic import ANTHROPIC_PRICING
from trace_harness.models.base import ActionKind, AgentAction, Message, ToolSpec
from trace_harness.models.cassette import CassetteConfig
from trace_harness.runner.batch import (
    BUDGET_EXHAUSTED,
    BUDGET_UNENFORCEABLE,
    BatchRunner,
    BatchSummary,
    BudgetGuard,
)
from trace_harness.runner.config import RunConfig
from trace_harness.runner.suite import AgentConfig, SuiteSpec, load_suite
from trace_harness.tracing.artifact_store import ArtifactStore

TASKS = [str(VALID_TASK_PATH), str(FAILURE_TASK_PATH), str(VALID_TASK_PATH)]

# 10k input and 10k output tokens on claude-sonnet-5 is $0.18 a run.
USAGE = {"input_tokens": 10_000, "output_tokens": 10_000}
RUN_COST = (10_000 * ANTHROPIC_PRICING["claude-sonnet-5"][0] + 10_000 * 15.0) / 1_000_000


class _PricedStub:
    """Answers at once like the Anthropic adapter, usage included unless told not to."""

    name = "anthropic"

    def __init__(self, usage: dict[str, int] | None) -> None:
        self.usage = usage

    def next_action(self, transcript: list[Message], tools: list[ToolSpec]) -> AgentAction:
        raw: dict[str, Any] = {"stop_reason": "end_turn"}
        if self.usage is not None:
            raw["usage"] = self.usage
        return AgentAction(kind=ActionKind.FINAL_ANSWER, final_answer="No refund.", raw=raw)


@pytest.fixture
def adapters(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every live adapter the batch builds, by provider. Fixture runs pass through."""
    from trace_harness.models import create_model_adapter as real_create

    built: list[str] = []

    def create(provider: str, **kwargs: Any):
        if provider == "fixture" or kwargs.get("cassette") is not None:
            return real_create(provider, **kwargs)
        built.append(provider)
        usage = None if kwargs["model"] == "claude-no-usage" else USAGE
        return _PricedStub(usage)

    monkeypatch.setattr("trace_harness.runner.pipeline.create_model_adapter", create)
    return built


def _suite(cap: float | None, *configs: AgentConfig, tasks: list[str] = TASKS) -> SuiteSpec:
    return SuiteSpec(
        suite_id="budget_probe",
        tasks=tasks,
        agent_configs=list(configs)
        or [AgentConfig(label="claude", provider="anthropic", model="claude-sonnet-5")],
        max_cost_usd=cap,
    )


def test_a_one_cent_cap_stops_the_batch_after_the_first_run(
    tmp_path: Path, adapters: list[str]
) -> None:
    store = ArtifactStore(tmp_path / "runs")
    summary = BatchRunner(store).run(_suite(0.01))

    assert len(summary.entries) == 1
    assert adapters == ["anthropic"]
    assert summary.entries[0].cost_usd == pytest.approx(RUN_COST)
    budget = summary.budget
    assert budget is not None
    assert budget.stop_reason == BUDGET_EXHAUSTED
    assert budget.spent_usd == pytest.approx(RUN_COST)
    assert budget.max_cost_usd == 0.01
    assert [(c.agent_label, c.task_path) for c in budget.not_run] == [
        ("claude", TASKS[1]),
        ("claude", TASKS[2]),
    ]
    # The durable summary says so too, where a dashboard or a sweep reads it.
    on_disk = json.loads(store.batch_summary_path(summary.batch_id).read_text())
    assert on_disk["budget"]["stop_reason"] == "budget_exhausted"


def test_a_batch_within_its_cap_runs_every_cell(tmp_path: Path, adapters: list[str]) -> None:
    summary = BatchRunner(ArtifactStore(tmp_path / "runs")).run(_suite(1.0))
    assert len(summary.entries) == 3
    assert summary.budget is not None
    assert summary.budget.stop_reason is None
    assert summary.budget.not_run == []
    assert summary.budget.spent_usd == pytest.approx(3 * RUN_COST)


def test_the_cap_is_checked_between_runs(tmp_path: Path, adapters: list[str]) -> None:
    """The second run starts with $0.18 spent, under the $0.30 cap, and crosses
    it. The third never starts, so the overshoot is at most one run."""
    summary = BatchRunner(ArtifactStore(tmp_path / "runs")).run(_suite(0.30))
    assert len(summary.entries) == 2
    assert summary.budget.stop_reason == BUDGET_EXHAUSTED
    assert len(summary.budget.not_run) == 1


def test_an_unpriced_live_model_under_a_cap_never_starts(
    tmp_path: Path, adapters: list[str]
) -> None:
    """A model with no price could pass the cap without anything seeing it."""
    config = AgentConfig(label="gemini", provider="gemini", model="gemini-not-in-the-table")
    summary = BatchRunner(ArtifactStore(tmp_path / "runs")).run(_suite(5.0, config))
    assert adapters == []
    assert summary.entries == []
    assert summary.budget.stop_reason == BUDGET_UNENFORCEABLE
    assert "gemini-not-in-the-table" in summary.budget.detail
    assert len(summary.budget.not_run) == 3


def test_a_live_run_with_no_recorded_cost_stops_the_batch(
    tmp_path: Path, adapters: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A null cost is never counted as zero."""
    config = AgentConfig(label="claude", provider="anthropic", model="claude-no-usage")
    # Priced by name so the guard admits it; the stub then reports no usage.
    monkeypatch.setitem(ANTHROPIC_PRICING, "claude-no-usage", (3.0, 15.0))
    summary = BatchRunner(ArtifactStore(tmp_path / "runs")).run(_suite(5.0, config))
    assert len(summary.entries) == 1
    assert summary.entries[0].cost_usd is None
    assert summary.budget.stop_reason == BUDGET_UNENFORCEABLE
    assert summary.entries[0].run_id in summary.budget.detail
    assert summary.budget.spent_usd == 0.0


def test_without_a_cap_nothing_changes(tmp_path: Path, adapters: list[str]) -> None:
    """Old suites run exactly as before: unpriced runs go ahead, cost stays null."""
    config = AgentConfig(label="gemini", provider="gemini", model="gemini-not-in-the-table")
    summary = BatchRunner(ArtifactStore(tmp_path / "runs")).run(_suite(None, config))
    assert len(summary.entries) == 3
    assert summary.budget is None
    assert all(entry.cost_usd is None for entry in summary.entries)


def test_fixture_runs_are_never_refused_even_under_a_zero_cap(tmp_path: Path) -> None:
    summary = BatchRunner(ArtifactStore(tmp_path / "runs")).run(
        _suite(0.0, AgentConfig(label="fixture"))
    )
    assert len(summary.entries) == 3
    assert summary.budget.stop_reason is None
    assert summary.budget.spent_usd == 0.0


def test_a_zero_cap_refuses_the_first_live_run(tmp_path: Path, adapters: list[str]) -> None:
    summary = BatchRunner(ArtifactStore(tmp_path / "runs")).run(_suite(0.0))
    assert adapters == []
    assert summary.budget.stop_reason == BUDGET_EXHAUSTED


def test_a_replay_is_free_and_never_refused_on_price(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replay calls nothing, so it costs zero and is never refused on price."""
    monkeypatch.chdir(REPO_ROOT)
    suite = load_suite(FIXTURES_DIR / "suites/refund_policy_gemini_replay.json")
    suite.max_cost_usd = 0.01
    summary = BatchRunner(ArtifactStore(tmp_path / "runs")).run(suite)
    assert [entry.status for entry in summary.entries] == ["completed"]
    assert summary.entries[0].cost_usd == 0.0
    assert summary.budget.stop_reason is None


def test_the_guard_is_usable_on_its_own() -> None:
    """run-sweep and branch will drive it directly, so its contract stands alone."""
    guard = BudgetGuard(0.5)
    assert guard.admit("anthropic", "claude-sonnet-5")
    guard.charge(0.3, "anthropic", run_id="run_a")
    assert guard.admit("anthropic", "claude-sonnet-5")
    guard.charge(0.3, "anthropic", run_id="run_b")
    assert not guard.admit("anthropic", "claude-sonnet-5")
    assert guard.stop_reason == BUDGET_EXHAUSTED
    # Once stopped, a free run is refused too: the batch has stopped.
    assert not guard.admit("fixture", None)
    # A cell that failed before any run existed spent nothing.
    fresh = BudgetGuard(0.5)
    fresh.charge(None, "anthropic", run_id=None)
    assert fresh.stop_reason is None
    # A record-mode cassette is live; replay is not.
    unpriced = "gemini-not-in-the-table"
    assert not BudgetGuard(1.0).admit("gemini", unpriced, CassetteConfig(mode="record"))
    assert BudgetGuard(1.0).admit("gemini", unpriced, CassetteConfig(mode="replay"))


def test_a_negative_cap_is_rejected() -> None:
    with pytest.raises(ValueError):
        _suite(-0.01)


# --- the CLI ------------------------------------------------------------------


def _write_suite(tmp_path: Path, cap: float | None, **config: Any) -> str:
    """Three cells of a task the stub's answer passes, so only the budget can fail it."""
    path = tmp_path / f"suite_{cap}.json"
    agent = {"label": "claude", "provider": "anthropic", "model": "claude-sonnet-5", **config}
    path.write_text(
        json.dumps(
            {
                "suite_id": "budget_cli",
                "tasks": [str(NO_REFUND_TASK_PATH)] * 3,
                "agent_configs": [agent],
                "max_cost_usd": cap,
            }
        )
    )
    return str(path)


def test_cli_reports_a_budget_stop_and_gates_on_it(
    tmp_path: Path, adapters: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    uncapped = _write_suite(tmp_path, None)
    gate = ["--fail-on-verifier"]
    assert main(["run-suite", uncapped, "--runs-dir", str(tmp_path / "control"), *gate]) == 0
    capsys.readouterr()

    suite = _write_suite(tmp_path, 0.01)
    assert main(["run-suite", suite, "--runs-dir", str(tmp_path / "a")]) == 0
    out = capsys.readouterr().out
    assert "budget_exhausted" in out
    assert "2 cell(s)" in out
    # In CI gate mode an early stop means not every cell was verified, even
    # though the one run that happened passed.
    assert main(["run-suite", suite, "--runs-dir", str(tmp_path / "b"), *gate]) == 1


def test_cli_exits_2_when_the_cap_cannot_be_enforced(
    tmp_path: Path, adapters: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    suite = _write_suite(tmp_path, 1.0, provider="gemini", model="gemini-3.6-flash")
    assert main(["run-suite", suite, "--runs-dir", str(tmp_path / "runs")]) == 2
    assert "budget_unenforceable" in capsys.readouterr().out


# --- older files still load ------------------------------------------------------


def test_older_suites_load_without_a_cap_or_policy() -> None:
    for name in ("multi_config.json", "refund_policy_gemini_replay.json", "refund_v0.json"):
        suite = load_suite(FIXTURES_DIR / "suites" / name)
        assert suite.max_cost_usd is None
        assert all(config.call_policy is None for config in suite.agent_configs)


def test_an_older_batch_summary_loads_without_a_budget(tmp_path: Path) -> None:
    summary = BatchRunner(ArtifactStore(tmp_path / "runs")).run(
        _suite(None, AgentConfig(label="fixture"), tasks=[str(VALID_TASK_PATH)])
    )
    old = json.loads(summary.model_dump_json())
    old["schema_version"] = "0.2.0"
    del old["budget"]
    loaded = BatchSummary.model_validate(old)
    assert loaded.budget is None
    assert loaded.entries == summary.entries


def test_every_retained_run_config_still_loads() -> None:
    paths = [
        path
        for root in ("docs", "fixtures", "apps/dashboard/src/fixtures")
        for path in (REPO_ROOT / root).rglob("run_config.json")
    ]
    assert len(paths) >= 10
    for path in paths:
        config = RunConfig.model_validate_json(path.read_text(encoding="utf-8"))
        assert config.call_policy is None, path
