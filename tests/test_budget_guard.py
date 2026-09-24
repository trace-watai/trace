"""The batch budget guard (#196): a spend cap that stops a batch, and what it records.

Offline. The live provider is a stub at the ``create_model_adapter`` seam that
answers the way the Anthropic adapter does, with a ``raw`` carrying usage, so
every run has a known price without an SDK, a key, or a network. Every price
is read from the adapter's table, so a price correction moves these tests with
it.
"""

from __future__ import annotations

import json
import threading
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
from trace_harness.models.base import (
    ActionKind,
    AgentAction,
    Message,
    ProviderNotConfiguredError,
    ToolSpec,
)
from trace_harness.models.cassette import CassetteConfig
from trace_harness.models.policy import CallRecord, FailedAttempt, ProviderCallError
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

# 10k input and 10k output tokens a run on claude-sonnet-5, priced from the
# adapter's own table.
USAGE = {"input_tokens": 10_000, "output_tokens": 10_000}
PRICE = ANTHROPIC_PRICING["claude-sonnet-5"]
RUN_COST = (USAGE["input_tokens"] * PRICE[0] + USAGE["output_tokens"] * PRICE[1]) / 1_000_000

#: Stub models that fail the way a live call can, keyed to the exception the
#: stub raises. Each is priced like claude-sonnet-5 so a cap admits it.
_FAILURES: dict[str, Any] = {}


def _failed_call(*statuses: int | None, outcome: str = "retries_exhausted") -> ProviderCallError:
    """The error the policy raises when it gives up, one failure per status."""
    record = CallRecord(
        attempts=len(statuses),
        outcome=outcome,
        failures=[
            FailedAttempt(
                attempt=number,
                error_class="APIConnectionError" if status is None else "APIStatusError",
                status_code=status,
                transient=outcome != "permanent_error",
            )
            for number, status in enumerate(statuses, 1)
        ],
    )
    return ProviderCallError(
        "Anthropic API call failed", call_record=record.model_dump(mode="json")
    )


class _PricedStub:
    """Answers at once like the Anthropic adapter, usage included unless told not to.

    ``fail`` makes it raise instead, as a live call that never got an answer
    does, and ``hang`` makes it block until released, as a call the runner
    abandons at its timeout does.
    """

    name = "anthropic"

    def __init__(
        self,
        usage: dict[str, int] | None,
        *,
        fail: Exception | None = None,
        hang: threading.Event | None = None,
    ) -> None:
        self.usage = usage
        self.fail = fail
        self.hang = hang

    def next_action(self, transcript: list[Message], tools: list[ToolSpec]) -> AgentAction:
        if self.hang is not None:
            self.hang.wait(10)
        if self.fail is not None:
            raise self.fail
        raw: dict[str, Any] = {"stop_reason": "end_turn"}
        if self.usage is not None:
            raw["usage"] = self.usage
        return AgentAction(kind=ActionKind.FINAL_ANSWER, final_answer="No refund.", raw=raw)


@pytest.fixture
def adapters(monkeypatch: pytest.MonkeyPatch):
    """Every live adapter the batch builds, by provider. Fixture runs pass through."""
    from trace_harness.models import create_model_adapter as real_create

    built: list[str] = []
    release = threading.Event()

    def create(provider: str, **kwargs: Any):
        if provider == "fixture" or kwargs.get("cassette") is not None:
            return real_create(provider, **kwargs)
        model = kwargs["model"]
        if model == "claude-not-configured":
            raise ProviderNotConfiguredError("ANTHROPIC_API_KEY is not set")
        built.append(provider)
        if model == "claude-hang":
            return _PricedStub(USAGE, hang=release)
        usage = None if model == "claude-no-usage" else USAGE
        return _PricedStub(usage, fail=_FAILURES.get(model))

    monkeypatch.setattr("trace_harness.runner.pipeline.create_model_adapter", create)
    yield built
    # Lets an abandoned call's daemon thread finish.
    release.set()
    _FAILURES.clear()


def _priced_stub_model(monkeypatch: pytest.MonkeyPatch, model: str) -> AgentConfig:
    """A stub model the guard admits, priced exactly like claude-sonnet-5."""
    monkeypatch.setitem(ANTHROPIC_PRICING, model, PRICE)
    return AgentConfig(label="claude", provider="anthropic", model=model)


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
    assert RUN_COST > 0.01
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
    summary = BatchRunner(ArtifactStore(tmp_path / "runs")).run(_suite(4 * RUN_COST))
    assert len(summary.entries) == 3
    assert summary.budget is not None
    assert summary.budget.stop_reason is None
    assert summary.budget.not_run == []
    assert summary.budget.spent_usd == pytest.approx(3 * RUN_COST)


def test_the_cap_is_checked_between_runs(tmp_path: Path, adapters: list[str]) -> None:
    """The second run starts with one run's cost spent, under a cap of one and
    a half runs, and crosses it. The third never starts, so the overshoot is at
    most one run."""
    summary = BatchRunner(ArtifactStore(tmp_path / "runs")).run(_suite(1.5 * RUN_COST))
    assert len(summary.entries) == 2
    assert summary.budget.stop_reason == BUDGET_EXHAUSTED
    assert len(summary.budget.not_run) == 1


def test_the_run_that_reaches_the_cap_is_the_stop_even_when_it_is_the_last(
    tmp_path: Path, adapters: list[str]
) -> None:
    """With no cell left to refuse, the summary still says the cap was reached,
    and by which run."""
    suite = _suite(1.5 * RUN_COST, tasks=TASKS[:2])
    summary = BatchRunner(ArtifactStore(tmp_path / "runs")).run(suite)
    assert len(summary.entries) == 2
    assert summary.budget.stop_reason == BUDGET_EXHAUSTED
    assert summary.budget.not_run == []
    assert summary.entries[1].run_id in summary.budget.detail
    assert summary.budget.spent_usd == pytest.approx(2 * RUN_COST)


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
    monkeypatch.setitem(ANTHROPIC_PRICING, "claude-no-usage", PRICE)
    summary = BatchRunner(ArtifactStore(tmp_path / "runs")).run(_suite(5.0, config))
    assert len(summary.entries) == 1
    assert summary.entries[0].cost_usd is None
    assert summary.budget.stop_reason == BUDGET_UNENFORCEABLE
    assert summary.entries[0].run_id in summary.budget.detail
    assert summary.budget.spent_usd == 0.0


@pytest.mark.parametrize(
    "failure",
    [
        _failed_call(503, 503, 503, 503, 503),
        _failed_call(400, outcome="permanent_error"),
        _failed_call(429, 529, outcome="deadline"),
        _failed_call(outcome="deadline"),
    ],
    ids=["retries_exhausted", "permanent_error", "deadline", "deadline_before_sending"],
)
def test_a_live_run_the_provider_refused_on_every_attempt_costs_nothing(
    tmp_path: Path, adapters: list[str], monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    """No answer ever came back, and every attempt got an HTTP status, so the
    provider billed nothing. One outage cell no longer ends a capped batch."""
    config = _priced_stub_model(monkeypatch, "claude-outage")
    _FAILURES["claude-outage"] = failure
    summary = BatchRunner(ArtifactStore(tmp_path / "runs")).run(_suite(1.0, config))
    assert [entry.status for entry in summary.entries] == ["error"] * 3
    assert [entry.cost_usd for entry in summary.entries] == [0.0] * 3
    assert summary.budget.stop_reason is None
    assert summary.budget.spent_usd == 0.0


# An answer arrived and was rejected after the call, but no response reached
# the trace: it was billed, and nothing says for how much.
_UNRECORDED_ANSWER = ProviderCallError(
    "Anthropic declined to respond",
    call_record=CallRecord(attempts=1, outcome="ok").model_dump(mode="json"),
)


@pytest.mark.parametrize(
    "failure",
    [
        _failed_call(None, None, None, None, None),
        _failed_call(503, None, outcome="deadline"),
        _UNRECORDED_ANSWER,
    ],
    ids=["connection_errors", "one_connection_error", "answer_rejected_unrecorded"],
)
def test_a_live_run_whose_failed_attempt_may_have_been_billed_has_no_cost(
    tmp_path: Path, adapters: list[str], monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    """A failure with no status may have reached the model before the
    connection dropped, so its cost is unknown and the capped batch stops."""
    config = _priced_stub_model(monkeypatch, "claude-dropped")
    _FAILURES["claude-dropped"] = failure
    summary = BatchRunner(ArtifactStore(tmp_path / "runs")).run(_suite(1.0, config))
    assert len(summary.entries) == 1
    assert summary.entries[0].cost_usd is None
    assert summary.budget.stop_reason == BUDGET_UNENFORCEABLE


def test_a_live_run_that_timed_out_has_no_cost(
    tmp_path: Path, adapters: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runner abandons the call at its timeout, and the provider may still
    bill it, so the cost stays unknown."""
    config = _priced_stub_model(monkeypatch, "claude-hang").model_copy(
        update={"timeout_seconds": 0.2}
    )
    summary = BatchRunner(ArtifactStore(tmp_path / "runs")).run(_suite(1.0, config))
    assert len(summary.entries) == 1
    assert summary.entries[0].termination_reason == "timeout"
    assert summary.entries[0].cost_usd is None
    assert summary.budget.stop_reason == BUDGET_UNENFORCEABLE


def test_a_live_run_whose_pipeline_fails_after_it_is_still_charged(
    tmp_path: Path, adapters: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run spent money before verification raised. The cell is a
    setup_error, but it keeps its run id and cost, and the guard counts it."""

    def crash(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("verifier crashed")

    monkeypatch.setattr("trace_harness.runner.pipeline._verify_run", crash)
    summary = BatchRunner(ArtifactStore(tmp_path / "runs")).run(_suite(0.01))
    assert len(summary.entries) == 1
    entry = summary.entries[0]
    assert entry.status == "setup_error"
    assert "verifier crashed" in entry.error
    assert entry.run_id is not None
    assert entry.cost_usd == pytest.approx(RUN_COST)
    assert summary.budget.stop_reason == BUDGET_EXHAUSTED
    assert summary.budget.spent_usd == pytest.approx(RUN_COST)
    assert len(summary.budget.not_run) == 2


def test_a_live_run_that_raises_inside_the_runner_is_priced_from_its_trace(
    tmp_path: Path, adapters: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_result.json could not be written, so the runner itself raised after
    the call was made. The trace it left still prices the run."""
    real_write = ArtifactStore.write_json

    def write_json(self: ArtifactStore, run_id: str, name: str, payload: Any) -> Path:
        if name == "run_result.json":
            raise OSError("disk full")
        return real_write(self, run_id, name, payload)

    monkeypatch.setattr(ArtifactStore, "write_json", write_json)
    summary = BatchRunner(ArtifactStore(tmp_path / "runs")).run(_suite(0.01))
    assert len(summary.entries) == 1
    assert summary.entries[0].status == "setup_error"
    assert summary.entries[0].cost_usd == pytest.approx(RUN_COST)
    assert summary.budget.stop_reason == BUDGET_EXHAUSTED


def test_a_live_run_that_raised_before_its_first_request_spends_nothing(
    tmp_path: Path, adapters: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run started and its trace exists, but no model prompt was ever
    prepared, so no provider was called and the capped batch goes on."""

    def broken_tools(self: Any) -> list[ToolSpec]:
        raise RuntimeError("tool registry failed")

    monkeypatch.setattr(
        "trace_harness.environment.support_env.SupportEnvironment.tool_specs", broken_tools
    )
    summary = BatchRunner(ArtifactStore(tmp_path / "runs")).run(_suite(0.01))
    assert [entry.status for entry in summary.entries] == ["setup_error"] * 3
    assert all(entry.run_id is not None for entry in summary.entries)
    assert [entry.cost_usd for entry in summary.entries] == [0.0] * 3
    assert summary.budget.stop_reason is None


def test_a_live_cell_that_fails_before_its_run_spends_nothing(
    tmp_path: Path, adapters: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No adapter, so no call: the setup error costs nothing and the next
    config still runs under the cap."""
    broken = _priced_stub_model(monkeypatch, "claude-not-configured")
    working = AgentConfig(label="working", provider="anthropic", model="claude-sonnet-5")
    summary = BatchRunner(ArtifactStore(tmp_path / "runs")).run(
        _suite(4 * RUN_COST, broken, working)
    )
    assert [entry.status for entry in summary.entries[:3]] == ["setup_error"] * 3
    assert all(entry.run_id is None for entry in summary.entries[:3])
    assert [entry.status for entry in summary.entries[3:]] == ["completed"] * 3
    assert summary.budget.stop_reason is None
    assert summary.budget.spent_usd == pytest.approx(3 * RUN_COST)


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
    # The run that reaches the cap is the stop, before anything else is asked.
    assert guard.stop_reason == BUDGET_EXHAUSTED
    assert "run_b" in guard.detail
    assert not guard.admit("anthropic", "claude-sonnet-5")
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
    suite = _write_suite(tmp_path, 1.0, provider="gemini", model="gemini-not-in-the-table")
    assert main(["run-suite", suite, "--runs-dir", str(tmp_path / "runs")]) == 2
    # Refused before it started: no adapter was ever built.
    assert adapters == []
    assert "budget_unenforceable" in capsys.readouterr().out


def test_cli_finishes_a_capped_suite_through_an_outage(
    tmp_path: Path,
    adapters: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every cell's call was refused with a status. That cost nothing, so the
    cap still holds and the suite is not a configuration error."""
    monkeypatch.setitem(ANTHROPIC_PRICING, "claude-outage", PRICE)
    _FAILURES["claude-outage"] = _failed_call(529, 529, 529, 529, 529)
    suite = _write_suite(tmp_path, 1.0, model="claude-outage")
    assert main(["run-suite", suite, "--runs-dir", str(tmp_path / "runs")]) == 0
    out = capsys.readouterr().out
    assert "budget_unenforceable" not in out
    assert "errored:" in out


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
