"""The experiment contract (#155).

The load-bearing test here is ``test_metric_names_match_the_memo``. The metric
names are a contract between this module, the #27 memo, and the dashboard
mirror, so the test reads the memo's appendix off disk. Restating the list here
would only prove the list equals itself.
"""

from __future__ import annotations

import json
import re

import pytest
from pydantic import ValidationError

from conftest import REPO_ROOT
from trace_harness.cli import main
from trace_harness.run_reader import RunReader
from trace_harness.runner.batch import BatchRunEntry, BatchSummary
from trace_harness.runner.experiment import (
    EXPERIMENT_SCHEMA_VERSION,
    Budget,
    ConditionKind,
    ConditionSpec,
    Decision,
    ExperimentMetrics,
    ExperimentResult,
    ExperimentSpec,
    FrozenManifest,
    MixedLiveModelsError,
    UnknownConditionError,
    derive_metrics,
    new_experiment_id,
    render_experiment_markdown,
    validate_condition_batches,
)
from trace_harness.runner.frozen_set import freeze
from trace_harness.runner.suite import AgentConfig
from trace_harness.tracing.artifact_store import ArtifactStore
from trace_harness.tracing.events import utc_now

MEMO = REPO_ROOT / "docs" / "methodology_metrics.md"


def _spec(**overrides) -> ExperimentSpec:
    base = {
        "experiment_id": "exp_20260101T000000Z_baseline",
        "hypothesis": "static replay agrees with live continuation on control verdicts",
        "frozen_manifest": FrozenManifest(
            suite_id="refund_bundles_v0",
            verifier_ids=["refund_policy"],
            fixtures_hash="sha256:deadbeef",
        ),
        "conditions": [
            ConditionSpec(
                name="replay_only",
                kind=ConditionKind.STATIC_REPLAY,
                agent_config=AgentConfig(label="fixture-baseline"),
            )
        ],
        "budget": Budget(max_runs=10, max_cost_usd=0.0),
    }
    return ExperimentSpec(**{**base, **overrides})


def _frozen(spec: ExperimentSpec) -> ExperimentSpec:
    """``spec`` as ``experiment freeze`` leaves it, hashed from this checkout.

    ``experiment record`` refuses a plan past schema 0.1.0 without a frozen
    set, and checks the set against the working directory, so a test that
    records this plan runs from the repository root.
    """
    manifest = spec.frozen_manifest
    frozen = freeze(REPO_ROOT, suite_id=manifest.suite_id, labels_path=manifest.labels_path)
    data = spec.model_dump(mode="json")
    data["frozen_manifest"]["frozen_set"] = {n: c.model_dump() for n, c in frozen.items()}
    data["frozen_manifest"]["fixtures_hash"] = frozen["fixtures"].digest
    return ExperimentSpec.model_validate(data)


# --- the contract with the #27 memo ---


def test_metric_names_match_the_memo() -> None:
    """The memo's appendix is the source of truth for the metric field set."""
    block = re.search(
        r"## Appendix\. Experiment metric names.*?```\n(.*?)```", MEMO.read_text(), re.S
    )
    assert block, "the #27 memo must carry an appendix listing the metric names"
    memo_names = [line.strip() for line in block.group(1).splitlines() if line.strip()]

    assert ExperimentMetrics.memo_field_names() == memo_names


def test_no_combined_score_field() -> None:
    """The memo is explicit that there is no single number; keep it that way."""
    names = ExperimentMetrics.memo_field_names()
    assert not any(n in {"score", "overall", "combined_score", "index"} for n in names)


# --- round trip ---


def test_both_files_round_trip(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "runs")
    spec = _spec()
    result = ExperimentResult(
        experiment_id=spec.experiment_id,
        condition_batches={"replay_only": "batch_20260101T000000Z_aaaaaaaa"},
        metrics=ExperimentMetrics(verified_failure_count=5, cost_usd=0.0),
        decision=Decision.BASELINE,
        decided_by="human",
    )
    store.write_experiment_spec(spec.experiment_id, spec)
    store.write_experiment_result(
        spec.experiment_id, result, markdown=render_experiment_markdown(spec, result)
    )

    back_spec = ExperimentSpec.model_validate(store.read_experiment_spec(spec.experiment_id))
    back_result = ExperimentResult.model_validate(store.read_experiment_result(spec.experiment_id))

    assert back_spec.model_dump(mode="json") == spec.model_dump(mode="json")
    assert back_result.model_dump(mode="json") == result.model_dump(mode="json")
    assert back_spec.schema_version == EXPERIMENT_SCHEMA_VERSION
    assert store.experiment_report_path(spec.experiment_id).is_file()


def test_experiment_id_follows_the_batch_id_style() -> None:
    assert re.fullmatch(r"exp_\d{8}T\d{6}Z_[0-9a-f]{8}", new_experiment_id())


# --- what the contract refuses ---


def test_duplicate_condition_names_are_rejected() -> None:
    condition = ConditionSpec(
        name="same",
        kind=ConditionKind.LIVE,
        agent_config=AgentConfig(label="a"),
    )
    with pytest.raises(ValidationError, match="duplicate name"):
        _spec(conditions=[condition, condition.model_copy()])


def test_recording_an_undeclared_condition_is_rejected() -> None:
    spec = _spec()
    with pytest.raises(UnknownConditionError, match="not declared"):
        validate_condition_batches(spec, {"live_on": "batch_x"})


def test_unknown_field_on_the_plan_is_rejected() -> None:
    """extra='forbid' so a misspelled key fails instead of being ignored."""
    with pytest.raises(ValidationError):
        ExperimentSpec.model_validate(
            {**json.loads(_spec().model_dump_json()), "hypothesis_": "typo"}
        )


# --- metric derivation ---


def _summary(batch_id: str, entries: list[BatchRunEntry]) -> BatchSummary:
    from trace_harness.runner.batch import BatchAggregates

    completed = sum(1 for e in entries if e.status == "completed")
    now = utc_now()
    return BatchSummary(
        batch_id=batch_id,
        suite_id="refund_bundles_v0",
        started_at=now,
        finished_at=now,
        agent_configs=[AgentConfig(label="fixture-baseline")],
        entries=entries,
        aggregates=BatchAggregates(
            total=len(entries),
            completed=completed,
            terminated=0,
            errored=0,
            verifier_passed=sum(1 for e in entries if e.verifier_passed is True),
            verifier_failed=sum(1 for e in entries if e.verifier_passed is False),
            cost_recorded=sum(1 for e in entries if e.cost_usd is not None),
            known_cost_usd=sum(e.cost_usd or 0.0 for e in entries),
        ),
    )


def _entry(task_id: str, verdict: str, *, cost=None, latency=None) -> BatchRunEntry:
    return BatchRunEntry(
        run_id=f"run_{task_id}",
        task_id=task_id,
        task_path=f"fixtures/tasks/{task_id}.json",
        agent_label="fixture-baseline",
        provider="fixture",
        status="completed",
        verdict=verdict,
        verifier_passed=(verdict == "pass"),
        cost_usd=cost,
        latency_ms=latency,
    )


def test_derives_only_what_a_batch_can_support(tmp_path) -> None:
    """Metrics needing the branch stage stay None, since a zero would read as measured."""
    summary = _summary("b1", [_entry("t1", "fail"), _entry("t2", "pass")])
    metrics = derive_metrics([summary])

    assert metrics.verified_failure_count == 1
    assert metrics.first_post_fork_divergence_rate is None
    assert metrics.noise_floor_divergence_rate is None
    assert metrics.verdict_agreement_rate is None
    assert metrics.post_block_outcomes is None


def _branch_entry(condition, status, diverged, outcome) -> BatchRunEntry:
    return _entry("t", "fail").model_copy(
        update={
            "status": status,
            "condition": condition,
            "diverged": diverged,
            "post_block_outcome": outcome,
        }
    )


def _answered(kinds: dict[str, ConditionKind]) -> dict[str, ConditionSpec]:
    """Batch id to the condition it answered, each condition named after its batch."""
    return {
        batch_id: ConditionSpec(name=batch_id, kind=kind, agent_config=AgentConfig(label=batch_id))
        for batch_id, kind in kinds.items()
    }


def _rate_counts(extra: dict[str, int | float]) -> dict[str, int | float]:
    """The counts behind the live metrics, without #155's cost and per-condition keys."""
    return {k: v for k, v in extra.items() if "divergence" in k or k.startswith("live_")}


def test_branch_metrics_follow_the_memo_formulas() -> None:
    """diverged / completed per arm, labels counted over live control-on runs (#159)."""
    live = _summary(
        "on",
        [
            _branch_entry("live", "completed", True, "substitute_violation"),
            _branch_entry("live", "completed", False, "recovered"),
            _branch_entry("live", "completed", True, "substitute_violation"),
            # Incomplete: out of the rate's denominator, but its label still counts.
            _branch_entry("live", "terminated", True, "stalled"),
        ],
    )
    off = _summary(
        "off",
        [
            _branch_entry("off", "completed", False, "no_block_observed"),
            _branch_entry("off", "completed", True, "no_block_observed"),
        ],
    )
    swapped = _summary("swap", [_branch_entry("swap", "completed", True, "false_success")])
    kinds = {
        "on": ConditionKind.LIVE,
        "off": ConditionKind.LIVE_NO_CONTROL,
        "swap": ConditionKind.LIVE_SWAPPED,
    }

    metrics = derive_metrics([live, off, swapped], conditions=_answered(kinds))

    assert metrics.first_post_fork_divergence_rate == round(2 / 3, 4)
    assert metrics.noise_floor_divergence_rate == 0.5
    assert metrics.post_block_outcomes == {
        "recovered": 1,
        "stalled": 1,
        "substitute_violation": 2,
    }
    assert _rate_counts(metrics.extra) == {
        "first_post_fork_divergence_k": 2,
        "first_post_fork_divergence_n": 3,
        "noise_floor_divergence_k": 1,
        "noise_floor_divergence_n": 2,
    }
    # Counts stay integers on disk, where 2 == 2.0 would hide a float. Only the
    # per-condition medians are real numbers.
    written = ExperimentMetrics.model_validate_json(metrics.model_dump_json()).extra
    assert {type(v) for k, v in written.items() if not k.startswith("latency_ms_p50.")} == {int}
    assert '"first_post_fork_divergence_k":2,' in metrics.model_dump_json()
    assert derive_metrics([live, off]).first_post_fork_divergence_rate is None


def _model_summary(batch_id: str, provider: str, model: str | None, diverged: list[bool]):
    """A branch batch whose runs came from one provider and model."""
    entries = [
        _branch_entry("c", "completed", d, "substitute_violation").model_copy(
            update={"provider": provider, "model": model}
        )
        for d in diverged
    ]
    return _summary(batch_id, entries).model_copy(
        update={"agent_configs": [AgentConfig(label=batch_id, provider=provider, model=model)]}
    )


def test_live_metrics_never_pool_two_models() -> None:
    """A rate and its noise floor from different agents compare nothing (#159)."""
    kinds = {"on": ConditionKind.LIVE, "off": ConditionKind.LIVE_NO_CONTROL}
    gemini = _model_summary("on", "gemini", "gemini-3.6-flash", [True])
    for provider, model in (("anthropic", "claude-sonnet-5"), ("gemini", "gemini-2.5-flash")):
        other = _model_summary("off", provider, model, [False])
        with pytest.raises(MixedLiveModelsError, match=f"{provider} {model}"):
            derive_metrics([gemini, other], conditions=_answered(kinds))
        with pytest.raises(MixedLiveModelsError):
            derive_metrics(
                [gemini, other.model_copy(update={"batch_id": "on2"})],
                conditions=_answered({"on": ConditionKind.LIVE, "on2": ConditionKind.LIVE}),
            )

    # A model left to the provider's default is that default.
    default = _model_summary("off", "gemini", None, [False])
    metrics = derive_metrics([gemini, default], conditions=_answered(kinds))
    assert (metrics.first_post_fork_divergence_rate, metrics.noise_floor_divergence_rate) == (
        1.0,
        0.0,
    )
    # live_swapped is another model on purpose and feeds none of the three.
    swapped = _model_summary("swap", "anthropic", "claude-sonnet-5", [True])
    derive_metrics(
        [gemini, default, swapped],
        conditions=_answered({**kinds, "swap": ConditionKind.LIVE_SWAPPED}),
    )
    # One batch that ran two models is refused as well.
    two = gemini.model_copy(
        update={"agent_configs": [*gemini.agent_configs, AgentConfig(label="x", provider="openai")]}
    )
    with pytest.raises(MixedLiveModelsError, match="openai gpt-5"):
        derive_metrics([two], conditions=_answered({"on": ConditionKind.LIVE}))


def test_fixture_batches_stay_out_of_the_live_metrics_beside_a_real_model() -> None:
    """A harness check recorded next to Gemini batches would pull both rates toward it."""
    check = _summary("check", [_branch_entry("live", "completed", False, "recovered")] * 3)
    kinds = {
        "check": ConditionKind.LIVE,
        "on": ConditionKind.LIVE,
        "off": ConditionKind.LIVE_NO_CONTROL,
    }
    on = _model_summary("on", "gemini", None, [True, True])
    off = _model_summary("off", "gemini", None, [False, True])

    metrics = derive_metrics([check, on, off], conditions=_answered(kinds))

    assert metrics.first_post_fork_divergence_rate == 1.0
    assert metrics.noise_floor_divergence_rate == 0.5
    assert metrics.post_block_outcomes == {"substitute_violation": 2}
    assert _rate_counts(metrics.extra) == {
        "first_post_fork_divergence_k": 2,
        "first_post_fork_divergence_n": 2,
        "noise_floor_divergence_k": 1,
        "noise_floor_divergence_n": 2,
        "live_fixture_batches_excluded": 1,
    }
    # The other metrics still read every batch.
    assert metrics.verified_failure_count == 7

    # With no real model recorded, fixture batches are the live metrics.
    alone = derive_metrics([check], conditions=_answered({"check": ConditionKind.LIVE}))
    assert alone.first_post_fork_divergence_rate == 0.0
    assert alone.post_block_outcomes == {"recovered": 3}
    assert "live_fixture_batches_excluded" not in alone.extra


def test_a_continuation_script_needs_the_fixture_provider() -> None:
    with pytest.raises(ValidationError, match="continuation_script needs provider 'fixture'"):
        ConditionSpec(
            name="live",
            kind=ConditionKind.LIVE,
            agent_config=AgentConfig(label="g", provider="gemini"),
            continuation_script="script.json",
        )


def test_cost_is_none_when_nothing_recorded_it() -> None:
    """A null cost means unknown, exactly as the memo says."""
    metrics = derive_metrics([_summary("b1", [_entry("t1", "pass")])])
    assert metrics.cost_usd is None


def test_latency_p50_over_all_conditions() -> None:
    summary = _summary(
        "b1",
        [
            _entry("t1", "pass", latency=100.0),
            _entry("t2", "pass", latency=300.0),
            _entry("t3", "pass", latency=200.0),
        ],
    )
    assert derive_metrics([summary]).latency_ms_p50 == 200.0


# --- the CLI, end to end ---


def test_record_then_list(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.chdir(REPO_ROOT)  # the frozen set is hashed from the working tree
    runs_dir = tmp_path / "runs"
    store = ArtifactStore(runs_dir)
    batch_id = "batch_20260101T000000Z_aaaaaaaa"
    store.write_batch_summary(
        batch_id, _summary(batch_id, [_entry("t1", "fail"), _entry("t2", "pass")])
    )

    spec = _spec()
    plan = tmp_path / "experiment.json"
    plan.write_text(spec.model_dump_json(indent=2), encoding="utf-8")
    assert main(["experiment", "freeze", str(plan)]) == 0

    code = main(
        [
            "--runs-dir",
            str(runs_dir),
            "experiment",
            "record",
            str(plan),
            "--condition",
            f"replay_only={batch_id}",
            "--decision",
            "baseline",
        ]
    )
    assert code == 0

    reader = RunReader(store)
    (listed,) = reader.list_experiments()
    assert listed.experiment_id == spec.experiment_id
    _, result = reader.get_experiment(spec.experiment_id)
    assert result is not None
    assert result.condition_batches == {"replay_only": batch_id}
    assert result.metrics.verified_failure_count == 1
    assert result.frozen_set_verified and not result.frozen_set_drifted

    capsys.readouterr()
    assert main(["--runs-dir", str(runs_dir), "list-experiments"]) == 0
    assert spec.experiment_id in capsys.readouterr().out


def test_record_rejects_a_condition_the_plan_never_declared(tmp_path, capsys) -> None:
    runs_dir = tmp_path / "runs"
    plan = tmp_path / "experiment.json"
    plan.write_text(_spec().model_dump_json(indent=2), encoding="utf-8")
    capsys.readouterr()

    code = main(
        [
            "--runs-dir",
            str(runs_dir),
            "experiment",
            "record",
            str(plan),
            "--condition",
            "live_on=batch_x",
        ]
    )
    assert code == 2
    assert "not declared" in capsys.readouterr().err


def test_list_experiments_on_an_empty_dir(tmp_path, capsys) -> None:
    assert main(["--runs-dir", str(tmp_path / "runs"), "list-experiments"]) == 0
    assert "no experiments found" in capsys.readouterr().out


# --- the retained baseline ---


def test_retained_baseline_is_readable() -> None:
    """The committed baseline is what proves the contract before #159 exists."""
    # docs/acceptance holds the retained runs, batches and experiments as
    # siblings, so the store's root is docs/acceptance itself.
    reader = RunReader(ArtifactStore(REPO_ROOT / "docs" / "acceptance"))
    specs = reader.list_experiments()
    assert specs, "no retained experiment under docs/acceptance/experiments"

    spec, result = reader.get_experiment(specs[0].experiment_id)
    assert result is not None
    assert result.decision is Decision.BASELINE
    assert result.metrics.verified_failure_count == 5
    assert set(result.condition_batches) == {c.name for c in spec.conditions}
