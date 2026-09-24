"""run-sweep (#198) end to end, offline, with two fake live providers.

The fakes in tests/sweep_fakes.py fix every cell's behavior, so the pass
counts, flips, labels and costs below are known before the sweep runs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import FIXTURES_DIR, REPO_ROOT
from sweep_fakes import (
    MODELS,
    SEEDS,
    TASKS,
    A,
    B,
    C,
    FakeOpenAI,
    install_fakes,
    planned_cost,
    write_suite_and_spec,
)
from trace_harness.cli import main
from trace_harness.models import is_priced
from trace_harness.models.openai import OpenAINotConfiguredError
from trace_harness.runner.batch import BUDGET_EXHAUSTED, BatchRunner, BatchSummary
from trace_harness.runner.suite import AgentConfig, SuiteSpec, load_suite
from trace_harness.runner.sweep import (
    SweepLoadError,
    load_sweep,
    run_sweep,
    sweep_dir,
)
from trace_harness.runner.sweep_summary import NATURAL, STAGED_TRAP, SweepSummary
from trace_harness.tracing.artifact_store import ArtifactStore


@pytest.fixture
def fake_providers(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, int]]:
    return install_fakes(monkeypatch)


def _run(tmp_path: Path, **spec) -> tuple[SweepSummary, ArtifactStore]:
    store = ArtifactStore(tmp_path / "runs")
    summary = run_sweep(load_sweep(write_suite_and_spec(tmp_path, **spec)), store)
    return summary, store


def test_a_two_provider_sweep_counts_flips_costs_and_labels(tmp_path, fake_providers) -> None:
    summary, store = _run(tmp_path)

    rows = {(r.provider_label, r.task_path): (r.passed, r.failed, r.flipped) for r in summary.tasks}
    assert rows == {
        ("flash", A): (2, 1, True),
        ("flash", B): (3, 0, False),
        ("flash", C): (1, 2, True),
        ("mini", A): (3, 0, False),
        ("mini", B): (2, 1, True),
        ("mini", C): (0, 3, False),
    }
    assert [p.flipped_tasks for p in summary.providers] == [2, 1]
    assert summary.flipped_tasks == 3

    labels = {(c.provider_label, c.task_path, c.seed): c.label for c in summary.failing_cells}
    assert labels == {
        ("flash", A, 1): STAGED_TRAP,
        ("flash", C, 1): STAGED_TRAP,
        ("flash", C, 3): NATURAL,
        ("mini", B, 2): NATURAL,
        ("mini", C, 1): STAGED_TRAP,
        ("mini", C, 2): STAGED_TRAP,
        ("mini", C, 3): STAGED_TRAP,
    }
    by_cell = {(c.provider_label, c.task_path, c.seed): c for c in summary.failing_cells}
    assert by_cell[("mini", C, 1)].failed_check_ids == ["unauthorized_store_credit"]
    assert by_cell[("flash", C, 3)].natural_check_ids == ["final_answer_inconsistent_with_state"]

    cost = planned_cost("gemini") + planned_cost("openai")
    assert summary.cost_usd == pytest.approx(cost, abs=1e-6)
    assert [p.cost_usd for p in summary.providers] == pytest.approx(
        [planned_cost("gemini"), planned_cost("openai")], abs=1e-6
    )
    assert summary.verified_failures == 7
    assert summary.natural_verified_failures == 2
    assert summary.cost_per_verified_failure == pytest.approx(cost / 7, abs=1e-6)
    assert summary.cost_per_natural_verified_failure == pytest.approx(cost / 2, abs=1e-6)
    assert summary.budget.stop_reason is None
    assert summary.budget.spent_usd == pytest.approx(cost, abs=1e-6)

    # The durable summary says the same, and so does each failing cell's recording.
    root = sweep_dir(store.runs_dir, summary.sweep_id)
    on_disk = SweepSummary.model_validate_json((root / "sweep_summary.json").read_text())
    assert on_disk == summary
    for cell in summary.failing_cells:
        assert (root / cell.cassette_path).is_file()


def test_cells_run_seed_by_seed_across_providers(tmp_path, fake_providers) -> None:
    _run(tmp_path)
    order = list(dict.fromkeys((name, seed) for name, _, seed in fake_providers))
    assert order == [(p, s) for s in SEEDS for p in ("gemini", "openai")]


def test_sweep_and_suite_cells_take_one_public_cell_path(
    tmp_path, fake_providers, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sweep cell runs through BatchRunner.run_cell, as a run-suite cell does."""
    cells: list[tuple[str, str]] = []
    run_cell = BatchRunner.run_cell

    def counting(self, config, task_path):
        cells.append((config.label, task_path))
        return run_cell(self, config, task_path)

    monkeypatch.setattr(BatchRunner, "run_cell", counting)
    summary, _ = _run(tmp_path)
    assert len(cells) == summary.runs == len(TASKS) * len(SEEDS) * 2
    assert cells[0] == ("flash-seed1", A)

    cells.clear()
    suite = SuiteSpec(suite_id="probe", tasks=[B], agent_configs=[AgentConfig(label="fx")])
    BatchRunner(ArtifactStore(tmp_path / "suite")).run(suite)
    assert cells == [("fx", B)]


def test_a_cell_that_raises_after_its_run_is_charged(
    tmp_path, fake_providers, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pipeline that raises after the run keeps the run id and the trace's cost.

    Gemini's B seed 2 runs its three billed calls, then verification raises.
    The cell ends as a setup_error, and the sweep's budget and summary still
    count what it spent.
    """
    import trace_harness.runner.pipeline as pipeline

    verify = pipeline.verify_run

    def failing_verify(store, run_result, task):
        config = store.read_json(run_result.run_id, "run_config.json")
        if (config["provider"], task.task_id, config["seed"]) == (
            "gemini",
            "refund_policy_valid_cash",
            2,
        ):
            raise RuntimeError("verifier crashed after the run")
        return verify(store, run_result, task)

    monkeypatch.setattr(pipeline, "verify_run", failing_verify)
    summary, store = _run(tmp_path)

    flash = BatchSummary.model_validate(store.read_batch_summary(summary.providers[0].batch_id))
    [broken] = [e for e in flash.entries if e.status == "setup_error"]
    assert (broken.task_path, broken.seed) == (B, 2)
    assert broken.run_id is not None
    assert (store.runs_dir / broken.run_id / "trace.jsonl").is_file()
    three_calls = planned_cost("gemini", seeds=[2], tasks=[B])
    assert broken.cost_usd == pytest.approx(three_calls, abs=1e-9)

    cost = planned_cost("gemini") + planned_cost("openai")
    assert summary.budget.spent_usd == pytest.approx(cost, abs=1e-6)
    assert summary.cost_usd == pytest.approx(cost, abs=1e-6)
    assert summary.cost_recorded == summary.runs
    assert summary.cost_per_verified_failure == pytest.approx(cost / 7, abs=1e-6)


def test_each_provider_writes_one_batch_tagged_with_the_sweep(tmp_path, fake_providers) -> None:
    summary, store = _run(tmp_path)
    for provider in summary.providers:
        batch = BatchSummary.model_validate(store.read_batch_summary(provider.batch_id))
        assert batch.schema_version == "0.4.0"
        assert batch.metadata == {
            "sweep_id": summary.sweep_id,
            "sweep_name": "probe",
            "provider_label": provider.label,
        }
        assert len(batch.entries) == len(TASKS) * len(SEEDS)
        assert sorted({e.seed for e in batch.entries}) == SEEDS
        assert {e.model for e in batch.entries} == {provider.model}
        assert [c.seed for c in batch.agent_configs] == SEEDS
        assert all(c.cassette.mode == "record" for c in batch.agent_configs)


def test_one_budget_stops_the_sweep_across_providers(tmp_path, fake_providers) -> None:
    """Gemini's seed 1 spends the cap, so no OpenAI cell and no later seed runs."""
    seed_one = planned_cost("gemini", seeds=[1])
    summary, store = _run(tmp_path, max_cost_usd=seed_one - 1e-6)

    assert {(name, seed) for name, _, seed in fake_providers} == {("gemini", 1)}
    assert summary.runs == len(TASKS)
    budget = summary.budget
    assert budget.stop_reason == BUDGET_EXHAUSTED
    assert budget.spent_usd == pytest.approx(seed_one, abs=1e-6)
    assert len(budget.not_run) == len(TASKS) * (2 * len(SEEDS) - 1)
    flash, mini = summary.providers
    assert (flash.runs, flash.not_run, mini.runs, mini.not_run) == (3, 6, 0, 9)
    for provider in summary.providers:
        batch = BatchSummary.model_validate(store.read_batch_summary(provider.batch_id))
        assert batch.budget.stop_reason == BUDGET_EXHAUSTED
    assert sum(r.not_run for r in summary.tasks) == 15


def test_a_cap_reached_mid_seed_leaves_complete_seeds_differing_by_one(
    tmp_path, fake_providers
) -> None:
    """The cap is crossed by OpenAI's first cell of seed 2, after Gemini's seed 2."""
    spent = planned_cost("gemini", seeds=[1, 2]) + planned_cost("openai", seeds=[1])
    summary, _ = _run(tmp_path, max_cost_usd=spent + 1e-6)

    assert summary.budget.stop_reason == BUDGET_EXHAUSTED
    ran = {(name, seed) for name, _, seed in fake_providers}
    assert ran == {("gemini", 1), ("openai", 1), ("gemini", 2), ("openai", 2)}
    complete = {
        p.label: sorted(
            seed
            for seed in SEEDS
            if all(
                c.seed != seed for c in summary.budget.not_run if c.agent_label.startswith(p.label)
            )
        )
        for p in summary.providers
    }
    assert complete == {"flash": [1, 2], "mini": [1]}
    flash, mini = summary.providers
    assert (flash.runs, mini.runs) == (6, 4)


def test_an_unpriced_model_stops_the_sweep_before_any_call(tmp_path, fake_providers) -> None:
    spec = write_suite_and_spec(tmp_path)
    data = json.loads(spec.read_text())
    data["providers"][1]["model"] = "gpt-unpriced"
    spec.write_text(json.dumps(data))
    code = main(["run-sweep", str(spec), "--runs-dir", str(tmp_path / "runs")])
    assert code == 2
    assert fake_providers == []


def test_a_zero_cap_exits_2_before_any_call(tmp_path, fake_providers, capsys) -> None:
    spec = write_suite_and_spec(tmp_path, max_cost_usd=0)
    assert main(["run-sweep", str(spec), "--runs-dir", str(tmp_path / "runs")]) == 2
    assert "greater than 0" in capsys.readouterr().err
    assert fake_providers == []
    assert not (tmp_path / "runs").exists()


def test_a_missing_key_stops_the_sweep_before_any_call(
    tmp_path, fake_providers, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_key(self, *args, **kwargs):
        raise OpenAINotConfiguredError("OPENAI_API_KEY is not set.")

    monkeypatch.setattr(FakeOpenAI, "__init__", no_key)
    code = main(["run-sweep", str(write_suite_and_spec(tmp_path)), "--runs-dir", str(tmp_path)])
    assert code == 2
    assert fake_providers == []
    assert not (tmp_path / "sweeps").exists()


def test_a_refused_temperature_stops_the_sweep_before_any_call(
    tmp_path, fake_providers, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-flight adapter carries the spec's temperature, as every cell's would."""
    from trace_harness.models.openai import check_sampling

    fake_init = FakeOpenAI.__init__

    def checked(self, model=None, **knobs):
        check_sampling(model, knobs.get("temperature"))
        fake_init(self, model, **knobs)

    monkeypatch.setattr(FakeOpenAI, "__init__", checked)
    spec = write_suite_and_spec(tmp_path)
    data = json.loads(spec.read_text())
    data["providers"][1]["temperature"] = 0.5
    spec.write_text(json.dumps(data))
    code = main(["run-sweep", str(spec), "--runs-dir", str(tmp_path)])
    assert code == 2
    assert fake_providers == []
    assert not (tmp_path / "sweeps").exists()


def test_the_cli_prints_the_summary(tmp_path, fake_providers, capsys) -> None:
    spec = write_suite_and_spec(tmp_path)
    assert main(["run-sweep", str(spec), "--runs-dir", str(tmp_path / "runs")]) == 0
    out = capsys.readouterr().out
    assert "3 task(s) x 2 provider(s) x 3 seed(s) = 18 cell(s)" in out
    assert "verified failures:     7 (2 natural)" in out
    assert "flipped tasks:         3 of 3" in out
    assert "per natural failure:   $" in out
    assert len(list((tmp_path / "runs/sweeps").glob("*/sweep_summary.json"))) == 1


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("seeds", [1, 1], "seeds must be distinct"),
        ("max_cost_usd", None, "max_cost_usd"),
        ("max_cost_usd", 0, "greater than 0"),
        ("providers", [{"label": "f", "provider": "fixture", "model": "m"}], "live providers"),
        (
            "providers",
            [
                {"label": "a", "provider": "gemini", "model": "gemini-3.6-flash"},
                {"label": "b", "provider": "gemini", "model": "gemini-3.6-flash"},
            ],
            "models must be distinct",
        ),
    ],
)
def test_malformed_specs_are_refused(tmp_path, field, value, message) -> None:
    spec = write_suite_and_spec(tmp_path)
    data = json.loads(spec.read_text())
    data[field] = value
    spec.write_text(json.dumps(data))
    with pytest.raises(SweepLoadError, match=message):
        load_sweep(spec)


def test_the_committed_sweep_covers_refund_v0_under_two_vendors() -> None:
    spec = load_sweep(FIXTURES_DIR / "sweeps/refund_v0_live.json")
    assert spec.suite == "fixtures/suites/refund_v0.json"
    assert len(load_suite(REPO_ROOT / spec.suite).tasks) == 32
    assert len(spec.seeds) >= 5
    assert len({p.provider for p in spec.providers}) == 2
    assert all(is_priced(p.provider, p.model) for p in spec.providers)
    assert [p.model for p in spec.providers] == [MODELS["gemini"], MODELS["openai"]]
    assert 0 < spec.max_cost_usd
    assert "confirmation" in spec.budget_note
