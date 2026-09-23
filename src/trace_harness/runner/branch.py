"""The branch stage: continue a recorded run from a chosen step (#159).

Replay can only re-run what was recorded. The branch stage rebuilds the world
a regression artifact pinned, exactly as ``replay`` does, replays the
recorded actions through a condition's start step, and hands every later step
to the condition's agent with the condition's controls installed. Each run is
verified, attributed and bundled on failure the way ``run_task_pipeline``
does it, labelled with its post-block outcome (#157), and compared with the
recording after the fork.

One batch per condition lands under ``runs/batches/``, tagged with the
experiment id and condition name, which is what ``experiment record`` reads.
``static_replay`` conditions reuse the ``replay --apply-control`` path in the
CLI, and :func:`replay_batch` records that verdict as a batch of one.

The plan's ``budget.max_cost_usd`` caps what one ``branch`` invocation spends
on live calls, across every condition and seed, through the #196
:class:`~trace_harness.runner.batch.BudgetGuard`. :func:`admit_before_any_run`
asks it once per live condition before anything runs, and :func:`run_branch`
asks it before each live seed and charges it after. A seed that calls no
provider (the fixture provider, or a cassette replay) costs nothing and is
never refused. Each batch's ``budget`` block records what that condition spent
and, when the guard stopped it, why.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from trace_harness.attribution.post_block import classify_post_block_outcome
from trace_harness.environment.controls import select_controls
from trace_harness.environment.support_env import SupportEnvironment
from trace_harness.models import (
    create_model_adapter,
    makes_live_calls,
    resolve_call_policy,
    resolve_model_name,
)
from trace_harness.models.base import ModelAdapter
from trace_harness.models.cassette import (
    CassetteRequestConfig,
    RecordingModelAdapter,
    cassette_path,
)
from trace_harness.models.fixture import FixtureModelAdapter, FixtureScript
from trace_harness.models.fork import ForkAdapter
from trace_harness.models.policy import CallPolicy
from trace_harness.regression.replay import material_action, pinned_initial_state, pinned_script
from trace_harness.regression.report import ReplayReport
from trace_harness.regression.schemas import RegressionArtifact
from trace_harness.runner.agent_runner import AgentRunner
from trace_harness.runner.batch import (
    BatchBudget,
    BatchRunEntry,
    BatchSummary,
    BudgetGuard,
    NotRunCell,
    aggregate_entries,
    entry_from_pipeline,
    new_batch_id,
)
from trace_harness.runner.config import PROMPT_VERSION, RunConfig
from trace_harness.runner.experiment import ConditionKind, ConditionSpec, ExperimentSpec
from trace_harness.runner.pipeline import PipelineResult, attribute_and_bundle, verify_run
from trace_harness.runner.result import RunResult
from trace_harness.runner.target_agent import EXTERNAL_PROVIDER
from trace_harness.tasks.loader import load_task
from trace_harness.tasks.schemas import TaskSpec
from trace_harness.tracing import artifact_store as names
from trace_harness.tracing.artifact_store import ArtifactStore
from trace_harness.tracing.events import TraceEventType, utc_now
from trace_harness.verifiers.base import VerifierResult

logger = logging.getLogger(__name__)

LIVE_KINDS = frozenset(
    {ConditionKind.LIVE, ConditionKind.LIVE_NO_CONTROL, ConditionKind.LIVE_SWAPPED}
)


@dataclass
class BranchResult:
    """One condition's batch, or why the condition was skipped."""

    condition: str
    summary: BatchSummary | None = None
    skipped: str | None = None


def load_artifact(path: Path | str) -> RegressionArtifact:
    return RegressionArtifact.model_validate_json(Path(path).read_text(encoding="utf-8"))


def validate_condition(artifact: RegressionArtifact, condition: ConditionSpec) -> int:
    """Check a condition against the artifact before anything runs; return its fork step.

    The fork step is the last step the recording answers, 0 when the condition
    declares no start. Unknown control ids fail here, before a sweep spends
    anything, and so does an outside agent (provider ``external``), which
    branch does not run.
    """
    select_controls(condition.control_ids)
    if condition.kind is not ConditionKind.STATIC_REPLAY and not artifact.pinned_agent_actions:
        raise ValueError("the artifact pins no agent actions, so there is nothing to fork")
    agent = condition.agent_config
    if agent.provider == EXTERNAL_PROVIDER:
        # The fork adapter hands the continuation to a model adapter at the
        # start step. An outside agent has its own loop and cannot be started
        # mid-run from a recorded prefix, so branching one is not supported (#210).
        raise ValueError(
            f"condition {condition.name!r}: branch does not run outside agents yet "
            f"(provider 'external', agent_ref {agent.agent_ref!r}); use a fixture or "
            "live provider for the condition"
        )
    if agent.provider == "fixture" and agent.cassette is not None:
        raise ValueError(f"condition {condition.name!r}: a fixture continuation takes no cassette")
    script = condition.continuation_script
    if script and not Path(script).is_file():
        raise ValueError(f"condition {condition.name!r}: continuation script not found: {script}")
    if condition.start is None:
        return 0
    if condition.start.source_run_id != artifact.source_run_id:
        raise ValueError(
            f"condition {condition.name!r} starts from run {condition.start.source_run_id}, "
            f"but the artifact pins run {artifact.source_run_id}"
        )
    if condition.kind is ConditionKind.STATIC_REPLAY:
        return 0  # replay re-runs the whole recording
    recorded = len(artifact.pinned_agent_actions)
    fork_step = condition.start.step_id
    if fork_step > recorded:
        raise ValueError(
            f"condition {condition.name!r} starts at step {fork_step}, "
            f"but the recording has {recorded} step(s)"
        )
    if agent.provider == "fixture" and not script and fork_step == recorded:
        raise ValueError(
            f"condition {condition.name!r}: the recording has nothing after step {fork_step} "
            "to continue with"
        )
    return fork_step


def post_fork_divergence(
    recorded: list[dict[str, Any]], actual: list[dict[str, Any]], fork_step: int
) -> tuple[int | None, bool | None]:
    """Where a run first left the recording after ``fork_step``, and whether its first action did.

    Both lists hold ``model_action`` payloads in step order, compared through
    :func:`material_action`, so reasoning never counts as divergence. A step
    only one side reached counts as a difference. ``diverged`` is None when
    the run took no action after the fork, since there was nothing to compare.
    """
    first_step: int | None = None
    for index in range(fork_step, max(len(recorded), len(actual))):
        before = material_action(recorded[index]) if index < len(recorded) else None
        after = material_action(actual[index]) if index < len(actual) else None
        if before != after:
            first_step = index + 1
            break
    if len(actual) <= fork_step:
        return first_step, None
    return first_step, first_step == fork_step + 1


def calls_a_provider(condition: ConditionSpec) -> bool:
    """Whether a condition's runs call a live provider, and so can cost money."""
    agent = condition.agent_config
    return condition.kind in LIVE_KINDS and makes_live_calls(agent.provider, agent.cassette)


def admit_before_any_run(guard: BudgetGuard, conditions: list[ConditionSpec]) -> None:
    """Ask the guard about every live condition once, before any condition runs.

    A live model with no price, or a cap of zero, stops the guard here, so no
    live run of the invocation starts and nothing is spent on a plan whose cap
    cannot hold. Conditions that call no provider still run afterwards.
    """
    for condition in conditions:
        if calls_a_provider(condition):
            agent = condition.agent_config
            guard.admit(agent.provider, _live_model(condition), agent.cassette)


def run_branch(
    artifact_path: Path | str,
    experiment: ExperimentSpec,
    condition: ConditionSpec,
    store: ArtifactStore,
    guard: BudgetGuard | None = None,
) -> BranchResult:
    """Run every seed of one live condition and write its batch.

    A condition that replays model calls from cassettes is skipped as a whole
    when any of its seeds has no recording, so a partial set of seeds never
    stands in for the planned sample.

    ``guard`` is shared by every condition of one ``branch`` invocation; a
    caller that passes none gets one built from the plan's ``max_cost_usd``.
    """
    if condition.kind not in LIVE_KINDS:
        raise ValueError(f"{condition.kind.value} conditions run through replay, see replay_batch")
    artifact = load_artifact(artifact_path)
    fork_step = validate_condition(artifact, condition)
    task = load_task(Path(artifact.task_fixture.replace("\\", "/")).resolve())
    task = task.model_copy(update={"initial_state": pinned_initial_state(artifact)})
    seeds = condition.seeds or [condition.agent_config.seed]

    missing = _missing_cassettes(condition, task.task_id, seeds)
    if missing:
        return BranchResult(condition.name, skipped=f"no cassette recorded at {', '.join(missing)}")

    if guard is None:
        guard = BudgetGuard(experiment.budget.max_cost_usd)
    agent = condition.agent_config
    live = calls_a_provider(condition)
    model = _live_model(condition) if live else None
    spent_before, stopped_before = guard.spent_usd, guard.stop_reason is not None

    started_at = utc_now()
    entries: list[BatchRunEntry] = []
    not_run: list[NotRunCell] = []
    for seed in seeds:
        if live and not guard.admit(agent.provider, model, agent.cassette):
            not_run.append(
                NotRunCell(agent_label=agent.label, task_path=artifact.task_fixture, seed=seed)
            )
            continue
        try:
            entry = _run_seed(artifact, task, experiment, condition, fork_step, seed, store)
        except Exception as exc:  # noqa: BLE001 (isolate the seed so the batch goes on)
            logger.warning("branch seed %s of %s failed: %s", seed, condition.name, exc)
            entry = _setup_error(artifact, condition, seed, exc)
        entries.append(entry)
        guard.charge(entry.cost_usd, agent.provider, agent.cassette, run_id=entry.run_id)

    budget = _budget_block(guard, spent_before, stopped_before, not_run)
    summary = _write_batch(
        store, experiment, condition, artifact, entries, started_at, budget=budget
    )
    return BranchResult(condition.name, summary=summary)


def replay_batch(
    report: ReplayReport,
    experiment: ExperimentSpec,
    condition: ConditionSpec,
    artifact_path: Path | str,
    store: ArtifactStore,
    started_at: datetime,
) -> BatchSummary:
    """Record a ``static_replay`` condition's replay as a batch of one.

    The entry is the replayed scenario run. The replay's own verdict, the exit
    code ``replay --apply-control`` would return, goes in the batch metadata.
    """
    artifact = load_artifact(artifact_path)
    run_id = report.scenario.run_id
    run = RunResult.model_validate(store.read_json(run_id, names.RUN_RESULT))
    verdict = VerifierResult.model_validate(store.read_json(run_id, names.VERIFIER_RESULT))
    config = RunConfig.model_validate(store.read_json(run_id, names.RUN_CONFIG))
    task = TaskSpec.model_validate(store.read_json(run_id, names.TASK_SPEC))
    block = classify_post_block_outcome(store.read_trace(run_id), verdict, run)
    entry = entry_from_pipeline(
        PipelineResult(task, config, run, verdict),
        condition.agent_config,
        artifact.task_fixture,
        store.runs_dir,
    ).model_copy(update={"condition": condition.name, "post_block_outcome": block.outcome})
    # A replay calls no provider, so it spends nothing and is never refused.
    return _write_batch(
        store,
        experiment,
        condition,
        artifact,
        [entry],
        started_at,
        budget=BatchBudget(max_cost_usd=experiment.budget.max_cost_usd, spent_usd=0.0),
        replay_exit_code=report.exit_code,
    )


def _run_seed(
    artifact: RegressionArtifact,
    task: TaskSpec,
    experiment: ExperimentSpec,
    condition: ConditionSpec,
    fork_step: int,
    seed: int | None,
    store: ArtifactStore,
) -> BatchRunEntry:
    environment = SupportEnvironment.from_task(task, docs=None)
    # Controls enter only as installed controls, so every block carries blocked_by.
    for control in select_controls(condition.control_ids):
        environment.install_control(control)
    agent = condition.agent_config
    # The live continuation calls through the #196 retry and pacing policy, and
    # run_config.json records it, exactly as run_task_pipeline does.
    call_policy = resolve_call_policy(agent.provider, agent.call_policy, agent.cassette)
    continuation, model = _continuation(
        artifact, condition, task.task_id, fork_step, seed, call_policy
    )
    prefix = FixtureModelAdapter(pinned_script(artifact, task.task_id))
    adapter = ForkAdapter(prefix, continuation, switch_at_step=fork_step)

    metadata: dict[str, Any] = {
        "task_fixture_path": artifact.task_fixture,
        "agent_label": agent.label,
        "replay_pinned_state": "true",
        "branch": {
            "experiment_id": experiment.experiment_id,
            "condition": condition.name,
            "source_run_id": artifact.source_run_id,
            "switch_at_step": fork_step,
        },
    }
    if environment.installed_controls:
        metadata["controls"] = [c.model_dump(mode="json") for c in environment.installed_controls]
    if isinstance(continuation, RecordingModelAdapter):
        metadata["cassette_path"] = str(continuation.path)
    config = RunConfig(
        task_id=task.task_id,
        provider=agent.provider,
        model=model,
        max_steps=agent.max_steps,
        timeout_seconds=agent.timeout_seconds,
        temperature=agent.temperature,
        seed=seed,
        prompt_version=agent.prompt_version or PROMPT_VERSION,
        cassette=agent.cassette,
        call_policy=call_policy,
        metadata=metadata,
    )
    run = AgentRunner(adapter, environment, store).run(task, config)
    verdict = verify_run(store, run, task)
    if verdict is not None and verdict.has_violations:
        attribute_and_bundle(store, run.run_id, task, run)

    trace = store.read_trace(run.run_id)
    actions = [e.payload for e in trace if e.event_type is TraceEventType.MODEL_ACTION]
    step, diverged = post_fork_divergence(artifact.pinned_agent_actions, actions, fork_step)
    block = classify_post_block_outcome(trace, verdict, run) if verdict is not None else None
    return entry_from_pipeline(
        PipelineResult(task, config, run, verdict), agent, artifact.task_fixture, store.runs_dir
    ).model_copy(
        update={
            "condition": condition.name,
            "seed": seed,
            "first_post_fork_divergence_step": step,
            "diverged": diverged,
            "post_block_outcome": block.outcome if block else None,
        }
    )


def _continuation(
    artifact: RegressionArtifact,
    condition: ConditionSpec,
    task_id: str,
    fork_step: int,
    seed: int | None,
    call_policy: CallPolicy | None = None,
) -> tuple[ModelAdapter, str]:
    """The adapter that answers after the fork, and the model name to record."""
    agent = condition.agent_config
    if agent.provider == "fixture":
        if condition.continuation_script:
            script_path = Path(condition.continuation_script)
            return FixtureModelAdapter.from_file(script_path), f"scripted:{script_path.stem}"
        script = FixtureScript(
            script_id=f"recorded_after_step_{fork_step}",
            task_id=task_id,
            description=f"Actions run {artifact.source_run_id} recorded after step {fork_step}.",
            actions=artifact.pinned_agent_actions[fork_step:],
        )
        return FixtureModelAdapter(script), f"scripted:{script.script_id}"
    model = resolve_model_name(agent.provider, agent.model, None)
    adapter = create_model_adapter(
        agent.provider,
        model=model,
        temperature=agent.temperature,
        seed=seed,
        timeout_seconds=agent.timeout_seconds,
        cassette=agent.cassette,
        task_id=task_id,
        prompt_version=agent.prompt_version or PROMPT_VERSION,
        call_policy=call_policy,
    )
    return adapter, model


def _budget_block(
    guard: BudgetGuard, spent_before: float, stopped_before: bool, not_run: list[NotRunCell]
) -> BatchBudget | None:
    """This condition's budget block, cut from a guard that spans the invocation.

    ``spent_usd`` is what this batch's live runs cost, so the blocks of one
    invocation add up to what it spent. The stop is recorded only when the guard
    refused a seed here or stopped during this condition.
    """
    if guard.max_cost_usd is None:
        return None
    stopped_here = guard.stop_reason is not None and (bool(not_run) or not stopped_before)
    return BatchBudget(
        max_cost_usd=guard.max_cost_usd,
        spent_usd=round(guard.spent_usd - spent_before, 6),
        stop_reason=guard.stop_reason if stopped_here else None,
        detail=guard.detail if stopped_here else None,
        not_run=not_run,
    )


def _live_model(condition: ConditionSpec) -> str:
    """The model a live condition runs, resolved as the adapter resolves it."""
    agent = condition.agent_config
    return resolve_model_name(agent.provider, agent.model, None)


def _missing_cassettes(
    condition: ConditionSpec, task_id: str, seeds: list[int | None]
) -> list[str]:
    agent = condition.agent_config
    if agent.cassette is None or agent.cassette.mode != "replay":
        return []
    model = resolve_model_name(agent.provider, agent.model, None)
    paths = [
        cassette_path(
            agent.cassette.directory,
            CassetteRequestConfig(
                task_id=task_id,
                provider=agent.provider,
                model=model,
                temperature=agent.temperature,
                seed=seed,
                timeout_seconds=agent.timeout_seconds,
                prompt_version=agent.prompt_version or PROMPT_VERSION,
            ),
        )
        for seed in seeds
    ]
    return [str(path) for path in paths if not path.is_file()]


def _setup_error(
    artifact: RegressionArtifact, condition: ConditionSpec, seed: int | None, exc: Exception
) -> BatchRunEntry:
    agent = condition.agent_config
    return BatchRunEntry(
        run_id=None,
        task_id=Path(artifact.task_fixture).stem,
        task_path=artifact.task_fixture,
        agent_label=agent.label,
        provider=agent.provider,
        model=agent.model,
        prompt_version=agent.prompt_version,
        status="setup_error",
        error=f"{type(exc).__name__}: {exc}",
        condition=condition.name,
        seed=seed,
    )


def _write_batch(
    store: ArtifactStore,
    experiment: ExperimentSpec,
    condition: ConditionSpec,
    artifact: RegressionArtifact,
    entries: list[BatchRunEntry],
    started_at: datetime,
    budget: BatchBudget | None = None,
    **extra: Any,
) -> BatchSummary:
    summary = BatchSummary(
        batch_id=new_batch_id(),
        suite_id=experiment.frozen_manifest.suite_id,
        started_at=started_at,
        finished_at=utc_now(),
        agent_configs=[condition.agent_config],
        entries=entries,
        aggregates=aggregate_entries(entries),
        budget=budget,
        metadata={
            "experiment_id": experiment.experiment_id,
            "condition": condition.name,
            "condition_kind": condition.kind.value,
            "source_run_id": artifact.source_run_id,
            "start": condition.start.model_dump(mode="json") if condition.start else None,
            **extra,
        },
    )
    store.write_batch_summary(summary.batch_id, summary)
    for entry in entries:
        if entry.run_id is None:
            continue
        try:
            store.enrich_index_entry_with_batch(entry.run_id, summary.batch_id)
        except Exception:  # noqa: BLE001 (the summary is the source of truth)
            logger.warning("batch index enrich failed for %s", entry.run_id)
    return summary
