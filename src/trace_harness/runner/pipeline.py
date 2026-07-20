"""run_task_pipeline: one task through the full failure pipeline, no printing.

The programmatic equivalent of the CLI's ``run-pipeline`` stage sequence
(run -> verify -> on failure attribute + bundle), but it returns structured
results instead of printing them, so a batch runner can aggregate across many
tasks. The CLI keeps its own print-heavy path; this is the reusable core.

Every stage writes the same artifacts to ``runs/{run_id}/`` the CLI writes, so
downstream consumers (dashboard, RunReader) see identical data regardless of
whether a run came from ``run-pipeline`` or ``run-suite``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from trace_harness.environment.support_env import SupportEnvironment
from trace_harness.models import create_model_adapter
from trace_harness.runner.agent_runner import AgentRunner
from trace_harness.runner.config import PROMPT_VERSION, RunConfig
from trace_harness.runner.result import RunResult
from trace_harness.runner.suite import AgentConfig
from trace_harness.tasks.loader import load_docs_for_task, load_task
from trace_harness.tasks.schemas import TaskSpec
from trace_harness.tracing import artifact_store as names
from trace_harness.tracing.artifact_store import ArtifactStore
from trace_harness.verifiers.base import VerifierInput, VerifierResult, merge_verifier_results
from trace_harness.verifiers.registry import get_verifier


@dataclass
class PipelineResult:
    """Everything the batch needs from one task run."""

    task: TaskSpec
    run_config: RunConfig
    run_result: RunResult
    verifier_result: VerifierResult | None  # None when the task declares no verifiers


def _repo_relative(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def _resolve_fixture_script(task: TaskSpec, task_path: Path) -> Path:
    script = task.metadata.get("fixture_script")
    if not script:
        raise ValueError(
            f"task '{task.task_id}' has no metadata.fixture_script; the fixture "
            "provider needs a script to replay"
        )
    return (task_path.parent / script).resolve()


def run_task_pipeline(
    task_path: Path | str,
    agent_config: AgentConfig,
    store: ArtifactStore,
    *,
    bundle_on_fail: bool = True,
) -> PipelineResult:
    """Run one task under one agent config and produce all pipeline artifacts."""
    task_path = Path(task_path).resolve()
    task = load_task(task_path)
    docs = load_docs_for_task(task, task_path)
    environment = SupportEnvironment.from_task(task, docs=docs)

    metadata: dict[str, str] = {
        "task_fixture_path": _repo_relative(task_path),
        "agent_label": agent_config.label,
    }
    if agent_config.provider == "fixture":
        script_path = _resolve_fixture_script(task, task_path)
        adapter = create_model_adapter("fixture", script_path=script_path)
        model = f"scripted:{script_path.stem}"
        metadata["fixture_script_path"] = _repo_relative(script_path)
    else:
        adapter = create_model_adapter(agent_config.provider, model=agent_config.model)
        model = agent_config.model

    config = RunConfig(
        task_id=task.task_id,
        provider=agent_config.provider,
        model=model,
        max_steps=agent_config.max_steps,
        timeout_seconds=agent_config.timeout_seconds,
        temperature=agent_config.temperature,
        seed=agent_config.seed,
        prompt_version=agent_config.prompt_version or PROMPT_VERSION,
        metadata=metadata,
    )
    run_result = AgentRunner(adapter, environment, store).run(task, config)

    verifier_result = _verify_run(store, run_result.run_id, task)
    if verifier_result is not None and not verifier_result.passed and bundle_on_fail:
        _attribute_and_bundle(store, run_result.run_id, task, run_result)

    return PipelineResult(
        task=task,
        run_config=config,
        run_result=run_result,
        verifier_result=verifier_result,
    )


def _verify_run(store: ArtifactStore, run_id: str, task: TaskSpec) -> VerifierResult | None:
    """Run the task's verifiers and persist the merged result. None if no verifiers."""
    if not task.verifier_ids:
        return None
    trace = store.read_trace(run_id)
    final_state = store.read_json(run_id, names.FINAL_STATE)
    results = [
        get_verifier(verifier_id).verify(
            VerifierInput.from_parts(task=task, trace=trace, final_state=final_state, run_id=run_id)
        )
        for verifier_id in task.verifier_ids
    ]
    merged = merge_verifier_results(results)
    store.write_json(run_id, names.VERIFIER_RESULT, merged)
    return merged


def _attribute_and_bundle(
    store: ArtifactStore, run_id: str, task: TaskSpec, run_result: RunResult
) -> None:
    """Attribute a verified failure and generate its failure bundle."""
    from trace_harness.attribution.heuristic import HeuristicAttributor
    from trace_harness.failure_bundles.generator import FailureBundleGenerator

    trace = store.read_trace(run_id)
    verifier_result = VerifierResult.model_validate(store.read_json(run_id, names.VERIFIER_RESULT))

    attribution = HeuristicAttributor().attribute(task, trace, verifier_result)
    store.write_json(run_id, names.ATTRIBUTION_RESULT, attribution)

    config_metadata = store.read_json(run_id, names.RUN_CONFIG).get("metadata", {})
    bundle = FailureBundleGenerator().generate(
        task=task,
        run_result=run_result,
        trace=trace,
        verifier_result=verifier_result,
        attribution=attribution,
        final_state=store.read_json(run_id, names.FINAL_STATE),
        initial_state=store.read_json(run_id, names.INITIAL_STATE),
        task_fixture_path=config_metadata.get("task_fixture_path"),
    )
    store.write_json(run_id, names.FAILURE_CARD, bundle.failure_card)
    store.write_json(run_id, names.REPAIR_PACKAGE, bundle.repair_package)
    store.write_json(run_id, names.REGRESSION_ARTIFACT, bundle.regression_artifact)
