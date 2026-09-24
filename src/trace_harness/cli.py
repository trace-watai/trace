"""trace-harness CLI: run fixtures and drive the failure pipeline.

Commands (each is one pipeline stage; ``run-pipeline`` chains them):

    trace-harness run-fixture  fixtures/tasks/refund_policy_failure.json
    trace-harness verify       runs/<run_id>
    trace-harness attribute    runs/<run_id>
    trace-harness bundle       runs/<run_id>
    trace-harness run-pipeline fixtures/tasks/refund_policy_failure.json
    trace-harness run-suite    fixtures/suites/refund_v0.json
    trace-harness collect-regressions docs/acceptance/runs
    trace-harness report-suite batch_<...>
    trace-harness branch       <regression_artifact.json> --experiment <experiment.json>
    trace-harness run-pipeline <task> --agent trace_harness.agents.langgraph_ref:agent

``run-suite`` runs many tasks across agent configs in one batch, isolating
per-run failures and writing a batch summary for dashboard metrics.
``report-suite`` (or ``run-suite --report``) rolls a finished batch's
artifacts into a per-batch report: which checks fired, which failure
categories, and which claimed failure modes never actually showed up.

Stages communicate only through run artifacts on disk — ``verify`` reads
exactly what ``run-fixture`` wrote — so any stage can be re-run later, and
the dashboard/API see the same data the pipeline used.

Exit codes: 0 success; 1 verifier failed AND --fail-on-verifier was passed
(CI gate mode); 2 usage or input errors (argparse errors, bad paths,
malformed fixtures, missing artifacts, cassette errors, a suite budget cap that
cannot be enforced). Without the flag a verified
failure exits 0 — finding failures is this tool succeeding.

argparse over typer: subcommands this simple don't justify a dependency.
Revisit if the CLI grows rich help/completions needs.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from trace_harness.config import HarnessConfig, load_env_file
from trace_harness.environment.control_library import (
    DEFAULT_CONTROL_LIBRARY,
    load_library,
    rollback_control,
)
from trace_harness.environment.controls import (
    ControlInstance,
    reference_controls,
    select_controls,
)
from trace_harness.environment.state import SupportState
from trace_harness.environment.support_env import SupportEnvironment
from trace_harness.failure_bundles.schemas import RepairPackage
from trace_harness.metrics.history import HISTORY_PATH as DEFAULT_HISTORY_PATH
from trace_harness.models import create_model_adapter, resolve_call_policy, resolve_model_name
from trace_harness.models.base import ProviderNotConfiguredError
from trace_harness.models.cassette import CassetteConfig, RecordingModelAdapter
from trace_harness.models.fixture import FixtureModelAdapter, FixtureScript
from trace_harness.regression.promotion import LibraryGateError, commit_controls
from trace_harness.regression.repair_validation import (
    ControlValidation,
    ControlVerdict,
    RepairValidation,
    ReRun,
    decide_verdict,
    skipped_control,
)
from trace_harness.regression.replay import (
    describe_action_drift,
    describe_state_drift,
    pinned_initial_state,
)
from trace_harness.regression.replay import pinned_script as build_pinned_script
from trace_harness.regression.report import ReplayCaseResult, ReplayReport
from trace_harness.regression.schemas import RegressionArtifact
from trace_harness.run_reader import RunReader
from trace_harness.runner.agent_runner import AgentRunner
from trace_harness.runner.batch import new_batch_id
from trace_harness.runner.config import PROMPT_VERSION, RunConfig
from trace_harness.runner.result import RunResult, RunStatus
from trace_harness.runner.target_agent import (
    EXTERNAL_PROVIDER,
    TargetAgent,
    load_target_agent,
    run_target_agent,
)
from trace_harness.tasks.loader import load_docs_for_task, load_task
from trace_harness.tasks.schemas import TaskSpec
from trace_harness.tracing import artifact_store as names
from trace_harness.tracing.artifact_store import ArtifactStore
from trace_harness.tracing.events import TraceEvent, TraceEventType, utc_now
from trace_harness.verifiers.base import (
    VerifierInput,
    VerifierResult,
    mark_incomplete,
    merge_verifier_results,
)
from trace_harness.verifiers.registry import get_verifier

logger = logging.getLogger("trace_harness")


class CliInputError(ValueError):
    """A user-input problem (bad path, malformed fixture, missing config).

    Subclasses ValueError so main()'s handler reports it cleanly with exit
    code 2. Never ``raise SystemExit("message")`` for these: that exits with
    status 1 — colliding with the --fail-on-verifier CI-gate code — and
    bypasses the error handler entirely.
    """


def _print(label: str, value: str) -> None:
    print(f"  {label:<22} {value}")


def _resolve_script_path(task: TaskSpec, task_path: Path, override: str | None) -> Path:
    """Script resolution order: --script flag, then task metadata.fixture_script."""
    if override:
        return Path(override)
    metadata_script = task.metadata.get("fixture_script")
    if metadata_script:
        return (task_path.parent / metadata_script).resolve()
    raise CliInputError(
        f"task '{task.task_id}' has no metadata.fixture_script and no --script "
        "was given; the fixture provider needs a script to replay"
    )


def _resolve_run_dir(run_path: str, runs_dir: Path) -> Path:
    """Accept a run-directory path, or a bare run id resolved under --runs-dir."""
    direct = Path(run_path)
    if direct.is_dir():
        return direct
    candidate = runs_dir / run_path
    if candidate.is_dir():
        return candidate
    raise CliInputError(
        f"run directory not found: tried {direct.resolve()} and {candidate.resolve()}"
    )


def _add_provider_args(parser: argparse.ArgumentParser) -> None:
    """Provider-selection flags shared by run-fixture and run-pipeline."""
    parser.add_argument(
        "--control-library",
        default=None,
        metavar="PATH",
        help="load active controls from a library",
    )
    parser.add_argument(
        "--provider",
        default="fixture",
        help="model provider: 'fixture' (scripted, default) or 'gemini'",
    )
    parser.add_argument(
        "--agent",
        default=None,
        metavar="MODULE:FACTORY",
        help="run an outside agent (provider 'external'); see docs/bring_your_own_agent.md",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="model name for real providers (e.g. gemini-3.6-flash); ignored by fixture",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="max wall-clock seconds for the whole run (default: 120)",
    )
    parser.add_argument("--temperature", type=float, default=None)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--cassette-mode", choices=("record", "replay"), default=None)
    parser.add_argument(
        "--cassette-dir", default=None, help="cassette root (default with mode: fixtures/cassettes)"
    )


def _run_fixture(
    args: argparse.Namespace,
    store: ArtifactStore,
    pinned_initial_state: dict[str, Any] | None = None,
    pinned_script: FixtureScript | None = None,
    controls: list[ControlInstance] | None = None,
) -> RunResult:
    task_path = Path(args.task_path).resolve()
    task = load_task(task_path)
    metadata: dict[str, Any] = {"task_fixture_path": _repo_relative(task_path)}

    if pinned_initial_state is None:
        docs = load_docs_for_task(task, task_path)
    else:
        # Regression replay: the artifact's pinned snapshot is the world, docs
        # included, so the live docs fixture is deliberately never read.
        task = task.model_copy(update={"initial_state": pinned_initial_state})
        docs = None
        metadata["replay_pinned_state"] = "true"

    environment = SupportEnvironment.from_task(
        task, docs=docs, control_library=getattr(args, "control_library", None)
    )
    # Guardrails enter only as installed controls, never raw hooks, so every
    # block they cause carries blocked_by in the trace.
    for instance in controls or []:
        environment.install_control(instance)
    if environment.installed_controls:
        metadata["controls"] = [c.model_dump(mode="json") for c in environment.installed_controls]

    cassette_mode = getattr(args, "cassette_mode", None)
    cassette_dir = getattr(args, "cassette_dir", None)
    if cassette_dir and cassette_mode is None:
        raise CliInputError("--cassette-dir requires --cassette-mode")
    cassette = (
        CassetteConfig(mode=cassette_mode, directory=cassette_dir or "fixtures/cassettes")
        if cassette_mode
        else None
    )
    temperature = getattr(args, "temperature", None)
    seed = getattr(args, "seed", None)
    agent_ref = getattr(args, "agent", None)
    provider = EXTERNAL_PROVIDER if agent_ref else args.provider
    agent = _external_agent(args, agent_ref, cassette) if provider == EXTERNAL_PROVIDER else None
    # A single live run uses the provider's default policy; suites can override it.
    # An outside agent makes its own model calls, so its run records none.
    call_policy = resolve_call_policy(provider, None, cassette)

    # The fixture provider replays a script; real providers (gemini) drive the
    # agent live and need no script — only the fixture path is required.
    if agent is not None:
        adapter = None
        model = args.model or agent.name
    elif args.provider == "fixture" and pinned_script is not None:
        if cassette is not None:
            raise CliInputError("regression replay cannot also use model cassettes")
        # Replaying pinned actions: the script file is not consulted at all, so
        # editing it cannot change what an existing regression asserts.
        adapter = FixtureModelAdapter(pinned_script)
        model = f"scripted:{pinned_script.script_id}"
        metadata["replay_pinned_script"] = "true"
    else:
        script_path = None
        if args.provider == "fixture":
            script_path = _resolve_script_path(task, task_path, args.script)
            metadata["fixture_script_path"] = _repo_relative(script_path)
        model = resolve_model_name(args.provider, args.model, script_path)
        adapter = create_model_adapter(
            args.provider,
            script_path=script_path,
            model=model,
            timeout_seconds=args.timeout,
            temperature=temperature,
            seed=seed,
            cassette=cassette,
            task_id=task.task_id,
            prompt_version=PROMPT_VERSION,
            call_policy=call_policy,
        )
        if isinstance(adapter, RecordingModelAdapter):
            metadata["cassette_path"] = _repo_relative(adapter.path)

    config = RunConfig(
        task_id=task.task_id,
        provider=provider,
        model=model,
        max_steps=args.max_steps,
        timeout_seconds=args.timeout,
        temperature=temperature,
        seed=seed,
        cassette=cassette,
        call_policy=call_policy,
        agent_ref=agent_ref,
        metadata=metadata,
    )
    if agent is not None:
        result = run_target_agent(agent, environment, store, task, config)
    else:
        runner = AgentRunner(adapter, environment, store)
        result = runner.run(task, config)

    print(f"\nRun complete: {task.task_id}")
    _print("run_id:", result.run_id)
    _print("status:", f"{result.status.value} ({result.termination_reason.value})")
    _print("steps_taken:", str(result.steps_taken))
    _print("artifacts:", str(store.run_dir(result.run_id)))
    _print("trace:", str(store.trace_path(result.run_id)))
    if result.error:
        _print("error:", result.error)
    if cassette is not None and result.status is RunStatus.ERROR:
        raise CliInputError(result.error or "cassette run failed")
    print(f"\nNext: trace-harness verify {store.run_dir(result.run_id)}")
    return result


def _external_agent(
    args: argparse.Namespace, agent_ref: str | None, cassette: CassetteConfig | None
) -> TargetAgent:
    """Load the outside agent for ``--agent``, refusing flags that cannot apply to it."""
    if not agent_ref:
        raise CliInputError("provider 'external' needs --agent package.module:factory")
    if args.provider not in ("fixture", EXTERNAL_PROVIDER):
        raise CliInputError(f"--agent runs provider 'external'; it cannot use '{args.provider}'")
    model_flags = [
        flag
        for flag, value in (
            ("--script", getattr(args, "script", None)),
            ("--cassette-mode", cassette),
            ("--temperature", getattr(args, "temperature", None)),
            ("--seed", getattr(args, "seed", None)),
        )
        if value is not None
    ]
    if model_flags:
        raise CliInputError(
            f"--agent cannot take {', '.join(model_flags)}; the outside agent owns its model"
        )
    return load_target_agent(agent_ref)


def _repo_relative(path: Path) -> str:
    """Best-effort repo-relative rendering for replay commands."""
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def _verify(run_dir: Path) -> tuple[VerifierResult, bool]:
    """Run the task's verifiers; returns (merged result, run_completed).

    ``run_completed`` matters for gating: a run that aborted before acting
    leaves an empty state with no violations, so a verifier PASS alone must
    never satisfy the CI gate — a broken agent that does nothing is not a
    passing agent.
    """
    store, run_id = ArtifactStore.for_run_path(run_dir)
    task = TaskSpec.model_validate(store.read_json(run_id, names.TASK_SPEC))
    trace = store.read_trace(run_id)
    final_state = store.read_json(run_id, names.FINAL_STATE)
    run_result = RunResult.model_validate(store.read_json(run_id, names.RUN_RESULT))
    run_completed = run_result.status is RunStatus.COMPLETED

    if not task.verifier_ids:
        raise CliInputError(f"task '{task.task_id}' declares no verifier_ids; nothing to verify")
    results = [
        get_verifier(verifier_id).verify(
            VerifierInput.from_parts(
                task=task,
                trace=trace,
                final_state=final_state,
                run_id=run_id,
            )
        )
        for verifier_id in task.verifier_ids
    ]
    merged = merge_verifier_results(results)
    if not run_completed:
        merged = mark_incomplete(
            merged,
            status=run_result.status.value,
            termination_reason=run_result.termination_reason.value,
        )
    store.write_json(run_id, names.VERIFIER_RESULT, merged)
    try:
        store.enrich_index_entry_with_verifier(run_id)
    except Exception:  # noqa: BLE001
        logger.exception(
            "run index verifier enrich failed for %s; verifier_result is the source of truth",
            run_id,
        )

    verdict = merged.verdict.value.upper()
    print(f"\nVerifier verdict for {run_id}: {verdict}")
    _print("verifier_id:", merged.verifier_id)
    _print("blocks_release:", str(merged.blocks_release))
    if merged.severity:
        _print("severity:", merged.severity.value)
    for check in merged.failed_checks:
        print(f"  ✗ [{check.severity.value}] {check.check_id} (steps {check.step_ids})")
        print(f"      expected: {check.expected}")
        print(f"      actual:   {check.actual}")
    for warning in merged.warnings:
        print(f"  ⚠ {warning}")
    _print("written:", str(store.artifact_path(run_id, names.VERIFIER_RESULT)))
    return merged, run_completed


def _attribute(run_dir: Path) -> bool:
    """Returns True if an attribution was produced (verifier had failed)."""
    from trace_harness.attribution.heuristic import HeuristicAttributor

    store, run_id = ArtifactStore.for_run_path(run_dir)
    task = TaskSpec.model_validate(store.read_json(run_id, names.TASK_SPEC))
    trace = store.read_trace(run_id)
    verifier_result = VerifierResult.model_validate(store.read_json(run_id, names.VERIFIER_RESULT))
    if not verifier_result.has_violations:
        print(
            f"\nVerifier verdict for {run_id} is {verifier_result.verdict.value} "
            f"with no recorded violations; nothing to attribute."
        )
        return False

    run_result = (
        RunResult.model_validate(store.read_json(run_id, names.RUN_RESULT))
        if store.exists(run_id, names.RUN_RESULT)
        else None
    )
    attribution = HeuristicAttributor().attribute(task, trace, verifier_result, run_result)
    store.write_json(run_id, names.ATTRIBUTION_RESULT, attribution)

    print(f"\nAttribution for {run_id} (heuristic, confidence {attribution.confidence:.2f}):")
    _print("root_cause_step:", str(attribution.root_cause_step))
    _print("missed_recovery_step:", str(attribution.missed_recovery_step))
    _print("first_irreversible:", str(attribution.first_irreversible_action_step))
    _print("symptoms_at_steps:", str(attribution.visible_symptom_steps))
    _print("primary_category:", attribution.primary_failure_category.value)
    _print(
        "contributing:",
        ", ".join(c.value for c in attribution.contributing_failure_categories) or "—",
    )
    block = (
        "" if attribution.block_step is None else f" (first block at step {attribution.block_step})"
    )
    _print("post_block_outcome:", f"{attribution.post_block_outcome}{block}")
    _print("written:", str(store.artifact_path(run_id, names.ATTRIBUTION_RESULT)))
    return True


def _bundle(run_dir: Path) -> bool:
    """Returns True if a bundle was produced (verifier had failed)."""
    from trace_harness.attribution.schemas import AttributionResult
    from trace_harness.failure_bundles.generator import FailureBundleGenerator

    store, run_id = ArtifactStore.for_run_path(run_dir)
    task = TaskSpec.model_validate(store.read_json(run_id, names.TASK_SPEC))
    run_result = RunResult.model_validate(store.read_json(run_id, names.RUN_RESULT))
    trace = store.read_trace(run_id)
    verifier_result = VerifierResult.model_validate(store.read_json(run_id, names.VERIFIER_RESULT))
    if not verifier_result.has_violations:
        print(
            f"\nVerifier verdict for {run_id} is {verifier_result.verdict.value} "
            f"with no recorded violations; no failure bundle to generate."
        )
        return False
    attribution = AttributionResult.model_validate(
        store.read_json(run_id, names.ATTRIBUTION_RESULT)
    )
    run_config = store.read_json(run_id, names.RUN_CONFIG)
    config_metadata = run_config.get("metadata", {})

    bundle = FailureBundleGenerator().generate(
        task=task,
        run_result=run_result,
        trace=trace,
        verifier_result=verifier_result,
        attribution=attribution,
        final_state=store.read_json(run_id, names.FINAL_STATE),
        initial_state=store.read_json(run_id, names.INITIAL_STATE),
        task_fixture_path=config_metadata.get("task_fixture_path"),
        agent_ref=run_config.get("agent_ref"),
    )
    store.write_json(run_id, names.FAILURE_CARD, bundle.failure_card)
    store.write_json(run_id, names.REPAIR_PACKAGE, bundle.repair_package)
    store.write_json(run_id, names.REGRESSION_ARTIFACT, bundle.regression_artifact)

    print(f"\nFailure bundle for {run_id}:")
    _print("failure_card:", str(store.artifact_path(run_id, names.FAILURE_CARD)))
    _print("repair_package:", str(store.artifact_path(run_id, names.REPAIR_PACKAGE)))
    _print("regression:", str(store.artifact_path(run_id, names.REGRESSION_ARTIFACT)))
    print(f"  controls: {', '.join(c.name for c in bundle.repair_package.controls)}")
    _print("blast_radius:", bundle.failure_card.blast_radius)
    return True


def _replay_drift_notes(
    artifact: RegressionArtifact,
    task: TaskSpec,
    task_path: Path,
    pinned_state: dict[str, Any],
) -> list[str]:
    """Report how the fixture's world and script differ from what was pinned.

    Best-effort and never fatal: the replay uses the pinned inputs either way,
    so a docs fixture or script that has since moved must not break the run.
    A load problem is reported as a drift note instead.
    """
    notes: list[str] = []
    try:
        live_state = SupportState.from_task(task, docs=load_docs_for_task(task, task_path))
        notes += describe_state_drift(pinned_state, live_state.snapshot())
    except (FileNotFoundError, KeyError, ValueError) as exc:
        notes.append(f"could not rebuild the fixture's world to compare: {exc}")

    if artifact.pinned_agent_actions:
        try:
            script_path = _resolve_script_path(task, task_path, None)
            live_script = FixtureScript.model_validate(
                json.loads(script_path.read_text(encoding="utf-8"))
            )
            notes += describe_action_drift(
                artifact.pinned_agent_actions,
                [action.model_dump(mode="json", exclude={"raw"}) for action in live_script.actions],
            )
        except (FileNotFoundError, KeyError, ValueError) as exc:
            notes.append(f"could not read the fixture script to compare: {exc}")
    return notes


def _prescribed_controls(
    artifact_path: Path,
    artifact: RegressionArtifact,
    task_id: str,
    controls: list[ControlInstance],
) -> tuple[dict[str, set[str]], str]:
    """Read prescriptions beside the input artifact, independently of output storage."""
    package_path = artifact_path.with_name(names.REPAIR_PACKAGE)
    try:
        package = RepairPackage.model_validate_json(package_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return (
            {
                c.provenance.repair_control: set(artifact.verifier_checks)
                for c in controls
                if c.provenance.repair_control is not None
            },
            "reference_controls",
        )
    if package.run_id != artifact.source_run_id or package.task_id != task_id:
        raise CliInputError("repair package does not match the regression artifact's run and task")
    prescribed = {}
    for control in package.controls:
        if control.name in prescribed:
            raise CliInputError(f"duplicate repair control name: {control.name}")
        linked = set(control.linked_verifier_checks)
        if linked - set(artifact.verifier_checks):
            raise CliInputError(
                f"repair control {control.name!r} links checks the artifact never pinned"
            )
        prescribed[control.name] = linked
    return prescribed, "repair_package"


def _instance_for_repair_control(name: str) -> ControlInstance | None:
    """The shipped control instance a repair control materializes as, if any."""
    for instance in reference_controls():
        if instance.provenance.repair_control == name:
            return instance
    return None


def _validate_controls(
    *,
    store: ArtifactStore,
    artifact: RegressionArtifact,
    prescribed: dict[str, set[str]],
    controls_source: str,
    controls: list[ControlInstance],
    task_fixture_args,
    pinned_state: dict[str, Any] | None,
    script,
) -> RepairValidation:
    """Validate each prescribed control in isolation and return the artifact.

    Each materializable control is installed on its own, the pinned scenario is
    replayed, and every positive sibling is re-run. Validating one at a time is
    the whole point: a bundle verdict cannot say which control earned it.
    """
    batch_id = new_batch_id()
    pinned_checks = set(artifact.verifier_checks)
    selected_ids = {c.control_id for c in controls}
    validations: list[ControlValidation] = []

    for name, expected_checks in prescribed.items():
        instance = _instance_for_repair_control(name)
        if instance is None:
            validations.append(skipped_control(name))
            print(f"  {name}: skipped (not materializable)")
            continue
        if instance.control_id not in selected_ids or not expected_checks:
            reason = (
                "not_selected: this control was excluded by --control"
                if instance.control_id not in selected_ids
                else "no_linked_checks: the prescription names no checks to validate"
            )
            validations.append(
                ControlValidation(control=name, verdict=ControlVerdict.SKIPPED, reason=reason)
            )
            print(f"  {name}: skipped ({reason})")
            continue

        pinned = _run_fixture(
            task_fixture_args(artifact.task_fixture),
            store,
            controls=[instance],
            pinned_initial_state=pinned_state,
            pinned_script=script,
        )
        pinned_merged, pinned_completed = _verify(store.run_dir(pinned.run_id))
        _tag_batch(store, pinned.run_id, batch_id)
        pinned_failed = {c.check_id for c in pinned_merged.failed_checks}
        introduced = {
            c.check_id
            for c in pinned_merged.failed_checks
            if c.check_id not in pinned_checks and c.blocks_release
        }
        originating = ReRun(
            run_id=pinned.run_id,
            task_id=pinned.task_id,
            verdict=pinned_merged.verdict.value.upper(),
            cleared_checks=sorted(expected_checks - pinned_failed) if pinned_completed else [],
            failed_checks=sorted(pinned_failed),
        )

        sibling_reruns: list[ReRun] = []
        failing_siblings: list[str] = []
        incomplete_siblings: list[str] = []
        for sibling in artifact.positive_sibling_tests:
            sib = _run_fixture(task_fixture_args(sibling.task_fixture), store, controls=[instance])
            sib_merged, sib_completed = _verify(store.run_dir(sib.run_id))
            _tag_batch(store, sib.run_id, batch_id)
            sib_failed = sorted(c.check_id for c in sib_merged.failed_checks)
            sibling_reruns.append(
                ReRun(
                    run_id=sib.run_id,
                    task_id=sib.task_id,
                    verdict=sib_merged.verdict.value.upper(),
                    failed_checks=sib_failed,
                )
            )
            if not sib_completed:
                incomplete_siblings.append(sibling.test_name)
            elif not sib_merged.passed:
                failing_siblings.append(sibling.test_name)

        verdict, reason = decide_verdict(
            expected_checks=expected_checks,
            pinned_failed_checks=pinned_failed,
            pinned_introduced_blocking=introduced,
            failing_siblings=failing_siblings,
            pinned_completed=pinned_completed,
            incomplete_siblings=incomplete_siblings,
        )
        validations.append(
            ControlValidation(
                control=name,
                verdict=verdict,
                reason=reason,
                guardrail_ref=instance.guardrail_ref,
                control_id=instance.control_id,
                originating_rerun=originating,
                sibling_reruns=sibling_reruns,
            )
        )
        print(f"  {name}: {verdict.value}" + (f" ({reason})" if reason else ""))

    validation = RepairValidation(
        run_id=artifact.source_run_id,
        test_name=artifact.test_name,
        batch_id=batch_id,
        controls_source=controls_source,
        controls=validations,
    ).rebuild_rollup()
    store.write_json(artifact.source_run_id, names.REPAIR_VALIDATION, validation)
    return validation


def _tag_batch(store: ArtifactStore, run_id: str, batch_id: str) -> None:
    """Group a validation re-run into its session; never fatal to the validation."""
    try:
        store.enrich_index_entry_with_batch(run_id, batch_id)
    except Exception:  # noqa: BLE001
        logger.debug("could not tag run %s into batch %s", run_id, batch_id)


def _replay(
    artifact_path: Path,
    store: ArtifactStore,
    *,
    apply_control: bool = False,
    control_ids: list[str] | None = None,
    fail_on_rejected: bool = False,
    control_library: Path | None = None,
    commit: bool = False,
) -> int:
    if commit and not apply_control:
        raise CliInputError("--commit requires --apply-control")
    if control_ids and not apply_control:
        raise CliInputError("--control requires --apply-control")
    if fail_on_rejected and not apply_control:
        raise CliInputError("--fail-on-rejected requires --apply-control")
    if commit:
        control_library = control_library or DEFAULT_CONTROL_LIBRARY
        if not artifact_path.with_name(names.REPAIR_PACKAGE).is_file():
            raise CliInputError(
                "--commit requires the originating repair package beside the artifact"
            )
    candidates = select_controls(control_ids) if apply_control else []
    active = []
    if control_library is not None and (control_library.exists() or not commit):
        active = load_library(control_library).active_controls()
    combined = {c.control_id: c for c in active}
    for control in candidates:
        if control.control_id in combined:
            existing = combined[control.control_id]
            if (
                existing.guardrail_ref != control.guardrail_ref
                or existing.rule_ref != control.rule_ref
                or existing.behavior_on_failure != control.behavior_on_failure
            ):
                raise CliInputError(f"conflicting control ID: {control.control_id}")
        else:
            combined[control.control_id] = control
    code = _replay_result(
        artifact_path,
        store,
        apply_control=apply_control or control_library is not None,
        control_ids=control_ids,
        fail_on_rejected=fail_on_rejected,
        installed_controls=sorted(combined.values(), key=lambda c: c.control_id),
        validate_individually=apply_control,
        validation_controls=candidates,
    )
    if code or not commit:
        return code

    def gate(path: Path, controls: list[ControlInstance]) -> list[Path]:
        run_dirs: list[Path] = []
        if _replay_result(
            path,
            store,
            apply_control=True,
            installed_controls=controls,
            validate_individually=False,
            replayed_run_dirs=run_dirs,
        ):
            raise LibraryGateError(f"proposed library failed regression {path}")
        return run_dirs

    artifact = RegressionArtifact.model_validate_json(artifact_path.read_bytes())
    validation_path = store.artifact_path(artifact.source_run_id, names.REPAIR_VALIDATION)
    assert control_library is not None
    try:
        library = commit_controls(control_library, artifact_path, validation_path, candidates, gate)
    except LibraryGateError as exc:
        print(f"\nControl library unchanged: {exc}")
        return 1
    print(f"\nCommitted controls to {control_library}: {len(library.active_controls())} active")
    return 0


def _replay_result(
    artifact_path: Path,
    store: ArtifactStore,
    *,
    apply_control: bool = False,
    control_ids: list[str] | None = None,
    fail_on_rejected: bool = False,
    installed_controls: list[ControlInstance] | None = None,
    validate_individually: bool = True,
    validation_controls: list[ControlInstance] | None = None,
    replayed_run_dirs: list[Path] | None = None,
) -> int:
    """Keep the public replay command's exit contract unchanged."""
    return _replay_with_report(
        artifact_path,
        store,
        apply_control=apply_control,
        control_ids=control_ids,
        fail_on_rejected=fail_on_rejected,
        installed_controls=installed_controls,
        validate_individually=validate_individually,
        validation_controls=validation_controls,
        replayed_run_dirs=replayed_run_dirs,
    ).exit_code


def _replay_with_report(
    artifact_path: Path,
    store: ArtifactStore,
    *,
    apply_control: bool = False,
    control_ids: list[str] | None = None,
    fail_on_rejected: bool = False,
    installed_controls: list[ControlInstance] | None = None,
    validate_individually: bool = True,
    validation_controls: list[ControlInstance] | None = None,
    replayed_run_dirs: list[Path] | None = None,
) -> ReplayReport:
    """Replay a regression artifact and assert the gate conditions hold.

    1. Re-runs the scenario **from the artifact's pinned inputs** — state,
       docs, and (schema 0.2.0+) the agent's recorded actions — not from the
       fixture files' current contents. The task fixture is still read for the
       two things no artifact pins: the tool subset and the verifier ids.
       Drift between pinned and current is reported but never changes the
       verdict.
    2. Asserts the verifier still reproduces the pinned failure — or, with
       ``apply_control``, that it no longer does.
    3. Runs each positive sibling fixture and asserts it passes (always,
       regardless of ``apply_control`` — a guardrail must not break
       legitimate behavior either). Siblings are named by path only, so they
       run from their live fixtures; nothing about them is pinned.

    Without ``apply_control``: this is a plain regression check. "Gate
    clear" means the known failure still reproduces exactly as pinned — the
    normal meaning for a regression suite, since a bug silently stopping
    reproduction usually means the fixture broke, not that the bug got fixed.

    With ``apply_control``: installs the reference controls (environment.
    controls, all of them unless ``control_ids`` narrows the set) on the
    environment before every run in this replay, and
    inverts the assertion — "gate clear" now requires that every pinned check
    stopped firing *and* that the control introduced no new blocking failure
    of its own. Both halves matter: a guardrail that blocks a harmful action
    while leaving the agent asserting it happened has moved the failure, not
    removed it, and must not read as a clear gate.

    A control only affects checks its guardrails actually cover (today:
    unauthorized_cash_refund). A fixture whose failure also depends on
    downstream narration (a ticket, a final answer) that the scripted agent
    repeats unconditionally will still fail on those other checks, because a
    guardrail can only change what happens in *state*, not what a fixed
    script says. See docs/regression_contract.md#control-flip-demo for a
    fixture built so that isn't a problem.

    Returns structured evidence and the existing command's 0/1 exit status.
    """
    # Flag and control-id errors are usage errors: fail before any output.
    if control_ids and not apply_control:
        raise CliInputError("--control requires --apply-control")
    if fail_on_rejected and not apply_control:
        raise CliInputError("--fail-on-rejected requires --apply-control")
    controls = (
        installed_controls
        if installed_controls is not None
        else select_controls(control_ids)
        if apply_control
        else []
    )
    individual_controls = controls if validation_controls is None else validation_controls

    data = json.loads(artifact_path.read_text(encoding="utf-8"))
    artifact = RegressionArtifact.model_validate(data)
    pinned_state = pinned_initial_state(artifact)
    # The task fixture is a hard requirement (tool subset + verifier ids), so a
    # missing one is a usage error (exit 2), not something to replay around.
    # Retained artifacts may have been written on Windows. Fixture paths are
    # portable repository paths, regardless of the machine doing the replay.
    task_path = Path(artifact.task_fixture.replace("\\", "/")).resolve()
    task = load_task(task_path)
    script = build_pinned_script(artifact, task.task_id)
    prescribed, controls_source = (
        _prescribed_controls(artifact_path, artifact, task.task_id, individual_controls)
        if apply_control and validate_individually
        else ({}, "repair_package")
    )

    print(f"\nReplaying regression: {artifact.test_name}")
    _print("source_run_id:", artifact.source_run_id)
    _print("severity:", str(artifact.severity))
    _print("blocks_release:", str(artifact.blocks_release))
    _print("apply_control:", str(apply_control))
    _print("replay_mode:", artifact.replay_mode)
    if apply_control and artifact.replay_mode == "live_required":
        print("  ⚠ static replay is insufficient; a live agent must continue from the block point.")
    _print("pinned inputs:", "state + docs" + (" + agent actions" if script else " (no actions)"))

    for note in _replay_drift_notes(artifact, task, task_path, pinned_state):
        print(f"  ⚠ fixture drift — {note}")

    if controls:
        _print("controls:", ", ".join(c.control_id for c in controls))
    gate_failed = False

    def _fixture_args(task_path: str) -> argparse.Namespace:
        return argparse.Namespace(
            task_path=task_path.replace("\\", "/"),
            script=None,
            provider="fixture",
            model=None,
            max_steps=16,
            timeout=120.0,
        )

    # Step 1: re-run the pinned scenario; verifier must produce the expected failures.
    stages = 3 if apply_control and validate_individually else 2
    print(f"\n[1/{stages}] Replaying pinned scenario (script from {artifact.task_fixture})")
    result = _run_fixture(
        _fixture_args(artifact.task_fixture),
        store,
        controls=controls,
        pinned_initial_state=pinned_state,
        pinned_script=script,
    )
    run_dir = store.run_dir(result.run_id)
    if replayed_run_dirs is not None:
        replayed_run_dirs.append(run_dir)
    merged, run_completed = _verify(run_dir)

    actual_ids = {c.check_id for c in merged.failed_checks}
    expected_ids = set(artifact.verifier_checks)

    def _case(name: str, run: RunResult, verdict: VerifierResult) -> ReplayCaseResult:
        return ReplayCaseResult(
            test_name=name,
            run_id=run.run_id,
            completed=run.status is RunStatus.COMPLETED,
            verifier_passed=verdict.passed,
            failed_checks=sorted(check.check_id for check in verdict.failed_checks),
            blocking_checks=sorted(
                check.check_id for check in verdict.failed_checks if check.blocks_release
            ),
        )

    scenario = _case(artifact.test_name, result, merged)
    siblings: list[ReplayCaseResult] = []

    if not run_completed:
        print("  FAIL: pinned replay did not complete; no regression verdict can be established")
        gate_failed = True
    elif apply_control:
        # With a control installed, "gate clear" flips: the pinned checks must
        # be GONE. But absence alone isn't enough — a guardrail that trades one
        # blocking failure for another hasn't fixed anything, so any new
        # blocking check fires the gate too.
        still_firing = expected_ids & actual_ids
        introduced = sorted(
            check.check_id
            for check in merged.failed_checks
            if check.check_id not in expected_ids and check.blocks_release
        )
        if still_firing:
            print(
                f"  FAIL: control was applied but {sorted(still_firing)} still fired "
                "— the guardrail did not eliminate this failure"
            )
            gate_failed = True
        else:
            print(f"  PASS: control eliminated {sorted(expected_ids)}; check(s) did not reproduce")
        if introduced:
            print(
                f"  FAIL: blocking check(s) {introduced} fired that this artifact never "
                "pinned — the control moved the failure rather than removing it"
            )
            gate_failed = True
    else:
        missing = expected_ids - actual_ids
        if merged.passed:
            print(f"  FAIL: expected verifier to fail on {sorted(expected_ids)} but run passed")
            gate_failed = True
        elif missing:
            print(f"  FAIL: expected checks {sorted(missing)} to fail — not seen in result")
            gate_failed = True
        else:
            print("  PASS: all expected checks failed as expected")

    # Step 2: each positive sibling must pass.
    if artifact.positive_sibling_tests:
        print(f"\n[2/{stages}] Running {len(artifact.positive_sibling_tests)} positive sibling(s)")
        for i, sibling in enumerate(artifact.positive_sibling_tests, 1):
            print(f"  [{i}] {sibling.test_name}: {sibling.task_fixture}")
            sib_result = _run_fixture(_fixture_args(sibling.task_fixture), store, controls=controls)
            sib_dir = store.run_dir(sib_result.run_id)
            if replayed_run_dirs is not None:
                replayed_run_dirs.append(sib_dir)
            sib_merged, _ = _verify(sib_dir)
            siblings.append(_case(sibling.test_name, sib_result, sib_merged))
            if not sib_merged.passed:
                failed = sorted(c.check_id for c in sib_merged.failed_checks)
                print(f"      FAIL: sibling must pass but failed on {failed}")
                gate_failed = True
            else:
                print("      PASS")

    # Per-control validation (#146). The bundle gate above answers "do these
    # controls together hold the line"; this answers which control earned it,
    # and writes the answer down instead of leaving it in stdout.
    validation = None
    if apply_control and validate_individually:
        print("\n[3/3] Validating each prescribed control on its own")
        validation = _validate_controls(
            store=store,
            artifact=artifact,
            prescribed=prescribed,
            controls_source=controls_source,
            controls=individual_controls,
            task_fixture_args=_fixture_args,
            pinned_state=pinned_state,
            script=script,
        )
        rollup = validation.rollup
        _print(
            "validation:",
            f"{rollup.accepted} accepted, {rollup.rejected} rejected, {rollup.skipped} skipped",
        )
        written = store.artifact_path(artifact.source_run_id, names.REPAIR_VALIDATION)
        _print("written:", str(written))

    if validation is not None:
        if validation.has_incomplete:
            print("  FAIL: per-control validation did not complete")
            gate_failed = True
        if fail_on_rejected and validation.has_rejection:
            print("  FAIL: --fail-on-rejected found rejected controls")
            gate_failed = True
    status = "FAIL — regression gate fired" if gate_failed else "PASS — regression gate clear"
    print(f"\nReplay result: {status}\n")
    return ReplayReport(
        exit_code=1 if gate_failed else 0,
        expected_checks=sorted(expected_ids),
        scenario=scenario,
        siblings=siblings,
    )


def _validate_fixtures(args: argparse.Namespace) -> int:
    """Validate every task fixture under a directory, recursively.

    Three docs already told people this command existed. It did not, the
    checker globbed the top level only, and nothing in the repo check called
    it, so a task under refund_task_families that no suite referenced could
    merge without ever being validated (#185).
    """
    from trace_harness.tasks.validation import validate_fixture_tree

    root = Path(args.path)
    if not root.is_dir():
        raise CliInputError(f"fixture directory not found: {root}")

    verdicts = validate_fixture_tree(root)
    if not verdicts:
        raise CliInputError(f"no task files found under {root}")

    print(f"\nValidating {len(verdicts)} task fixture(s) under {root}")
    failures = [v for v in verdicts if not v.ok]
    for verdict in failures:
        rel = verdict.path.relative_to(root)
        if verdict.is_counterexample:
            print(f"  {rel}: FAIL — counterexample is no longer flagged")
            continue
        for issue in verdict.errors:
            print(f"  {rel}: {issue.code} — {issue.message}")

    valid = sum(1 for v in verdicts if v.ok)
    _print("a1 valid/total:", f"{valid}/{len(verdicts)}")
    _print("counterexamples:", str(sum(1 for v in verdicts if v.is_counterexample)))
    if failures:
        _print("result:", f"FAIL ({len(failures)} file(s))")
        return 1
    _print("result:", "PASS")
    return 0


def _load_experiment_plan(path: str) -> tuple[Path, Any]:
    from trace_harness.runner.experiment import ExperimentSpec

    spec_path = Path(path)
    if not spec_path.is_file():
        raise CliInputError(f"experiment plan not found: {spec_path}")
    return spec_path, ExperimentSpec.model_validate(
        json.loads(spec_path.read_text(encoding="utf-8"))
    )


def _experiment_freeze(args: argparse.Namespace) -> int:
    """Hash the frozen set into a plan, once, before any condition runs (#195).

    Paths resolve against the working directory like every other CLI path, so
    this runs from the repository root. A plan that already carries a frozen
    set is refused: freezing it again after the evaluator moved would turn
    drift into a clean record.
    """
    from trace_harness.runner.experiment import EXPERIMENT_SCHEMA_VERSION, ExperimentSpec
    from trace_harness.runner.frozen_set import FrozenSetError, freeze
    from trace_harness.tracing.artifact_store import _atomic_write_text

    spec_path, spec = _load_experiment_plan(args.experiment_path)
    manifest = spec.frozen_manifest
    if manifest.frozen_set is not None:
        raise CliInputError(
            f"{spec_path} is already frozen; a plan is frozen once, before its conditions run"
        )
    try:
        frozen = freeze(Path.cwd(), suite_id=manifest.suite_id, labels_path=manifest.labels_path)
    except FrozenSetError as exc:
        raise CliInputError(str(exc)) from None

    data = spec.model_dump(mode="json")
    data["schema_version"] = EXPERIMENT_SCHEMA_VERSION
    data["frozen_manifest"]["frozen_set"] = {n: c.model_dump() for n, c in frozen.items()}
    data["frozen_manifest"]["fixtures_hash"] = frozen["fixtures"].digest
    spec = ExperimentSpec.model_validate(data)
    _atomic_write_text(spec_path, json.dumps(spec.model_dump(mode="json"), indent=2) + "\n")

    print(f"\nExperiment frozen: {spec.experiment_id}")
    for name, component in frozen.items():
        _print(
            f"  {name}:",
            f"{component.digest[:19]}  {len(component.files)} file(s) in {component.path}",
        )
    _print("written:", str(spec_path))
    return 0


def _experiment_record(args: argparse.Namespace, store: ArtifactStore) -> int:
    """Record which batch answered which condition, and what was decided.

    The plan is read, never written here. Recording cannot invent a condition:
    a ``--condition`` naming something the spec does not declare is a usage
    error, because a result that describes different arms than the plan is not
    a result for that experiment.

    Recording also recomputes the plan's frozen set (#195) and refuses, with
    the files listed, when anything differs. ``--allow-drift`` records anyway,
    marks the result drifted and forces its decision to review. A plan from
    schema 0.1.0 has no frozen set; it records, and the result says nothing
    was checked.
    """
    from trace_harness.runner.batch import BatchSummary
    from trace_harness.runner.experiment import (
        DecidedBy,
        Decision,
        ExperimentResult,
        UnknownConditionError,
        derive_metrics,
        render_experiment_markdown,
        validate_condition_batches,
    )
    from trace_harness.runner.frozen_set import render_changes
    from trace_harness.runner.repair_effectiveness import (
        build_repair_effectiveness,
        write_repair_effectiveness,
    )
    from trace_harness.runner.verdict_agreement import (
        pair_table,
        recorded_batches,
        run_ids,
        score_pairs,
    )

    spec_path, spec = _load_experiment_plan(args.experiment_path)

    condition_batches: dict[str, str] = {}
    for pair in args.condition or []:
        name, _, batch_id = pair.partition("=")
        if not name or not batch_id:
            raise CliInputError(f"--condition expects name=batch_id, got {pair!r}")
        # A repeated name would silently keep the last batch given for it.
        if name in condition_batches:
            raise CliInputError(
                f"--condition {name} is given twice ({condition_batches[name]} and {batch_id}); "
                "each condition is answered by one batch"
            )
        condition_batches[name] = batch_id
    try:
        validate_condition_batches(spec, condition_batches)
    except UnknownConditionError as exc:
        raise CliInputError(str(exc)) from None

    manifest = spec.frozen_manifest
    drift = _frozen_set_drift(
        spec,
        spec_path,
        allow_drift=args.allow_drift,
        refused="recording is refused",
        override="--allow-drift to record the result as drifted with decision review",
    )

    summaries = []
    declared = {c.name: c for c in spec.conditions}
    for name, batch_id in condition_batches.items():
        try:
            summary = store.read_batch_summary(batch_id)
        except FileNotFoundError as exc:
            raise CliInputError(str(exc)) from None
        # A branch batch names the condition it ran. Recording it under another
        # name would swap the arms, and with them the two divergence rates.
        produced_for = (summary.get("metadata") or {}).get("condition")
        if produced_for not in (None, name):
            raise CliInputError(
                f"batch {batch_id} ran condition {produced_for!r} and cannot answer {name!r}"
            )
        summaries.append(summary)

    # The live verdicts and B1 read each run's failed checks and their steps,
    # which a batch entry does not keep (#200).
    parsed = [BatchSummary.model_validate(s) for s in summaries]
    by_batch = {batch_id: declared[name] for name, batch_id in condition_batches.items()}
    verdicts = _verifier_results(store, run_ids(parsed))
    recorded = recorded_batches(parsed, by_batch)
    pairs = score_pairs(recorded, verdicts)
    repair = build_repair_effectiveness(spec.experiment_id, recorded, verdicts)
    result = ExperimentResult(
        experiment_id=spec.experiment_id,
        condition_batches=condition_batches,
        metrics=derive_metrics(parsed, conditions=by_batch, verifier_results=verdicts),
        decision=Decision.REVIEW if drift else Decision(args.decision),
        decided_by=DecidedBy(args.decided_by),
        report_path=str(store.experiment_report_path(spec.experiment_id)),
        metadata={"verdict_agreement_pairs": pair_table(pairs)} if pairs else {},
        frozen_set_verified=manifest.frozen_set is not None and not drift,
        frozen_set_drifted=bool(drift),
        frozen_set_drift=drift,
    )
    store.write_experiment_spec(spec.experiment_id, spec)
    store.write_experiment_result(
        spec.experiment_id, result, markdown=render_experiment_markdown(spec, result, repair)
    )
    sidecar = write_repair_effectiveness(store.experiment_dir(spec.experiment_id), repair)

    print(f"\nExperiment recorded: {spec.experiment_id}")
    _print("hypothesis:", spec.hypothesis)
    _print("decision:", f"{result.decision.value} (by {result.decided_by.value})")
    if result.frozen_set_drifted:
        _print("frozen set:", f"DRIFTED, {len(drift)} file(s), recorded with --allow-drift")
        for line in render_changes(drift):
            print(f"    {line}")
        if args.decision != Decision.REVIEW.value:
            _print("", f"--decision {args.decision} overridden: drift forces review")
    elif result.frozen_set_verified:
        _print("frozen set:", "verified, every frozen file matches the plan")
    else:
        _print("frozen set:", f"not recorded (plan schema {spec.schema_version})")
    for name, batch_id in sorted(condition_batches.items()):
        _print(f"  {name}:", batch_id)
    for metric in type(result.metrics).memo_field_names():
        value = getattr(result.metrics, metric)
        _print(f"  {metric}:", "not measured" if value is None else str(value))
    excluded = [p for p in pairs if p.excluded]
    if excluded:
        _print("excluded pairs:", f"{len(excluded)}, left out of verdict_agreement_rate")
        for pair in excluded:
            print(f"    {pair.kind} {pair.model} {pair.task_id} {pair.control}: {pair.excluded}")
    _print("repair effectiveness:", f"{len(repair.entries)} entr(ies) in {sidecar}")
    _print("written:", str(store.experiment_dir(spec.experiment_id)))
    return 0


def _verifier_results(store: ArtifactStore, ids: list[str]) -> dict[str, VerifierResult]:
    """Each named run's verifier result; a run without one is left for the metrics to exclude."""
    return {
        run_id: VerifierResult.model_validate(store.read_json(run_id, names.VERIFIER_RESULT))
        for run_id in ids
        if store.exists(run_id, names.VERIFIER_RESULT)
    }


def _frozen_set_drift(
    spec: Any, spec_path: Path, *, allow_drift: bool, refused: str, override: str
) -> list[Any]:
    """Recompute a plan's frozen set and refuse on drift unless allowed (#195).

    A 0.1.0 plan predates the frozen set and returns no drift. A later plan
    with no frozen set is refused, since nothing could show its evaluator held.
    """
    from trace_harness.runner.experiment import PRE_FROZEN_SET_SCHEMA_VERSION
    from trace_harness.runner.frozen_set import check_frozen_set, render_changes

    manifest = spec.frozen_manifest
    if manifest.frozen_set is None:
        if spec.schema_version != PRE_FROZEN_SET_SCHEMA_VERSION:
            raise CliInputError(
                f"{spec_path} has no frozen set; run `trace-harness experiment freeze "
                f"{spec_path}` before any condition runs"
            )
        return []
    drift = check_frozen_set(
        manifest.frozen_set,
        Path.cwd(),
        suite_id=manifest.suite_id,
        labels_path=manifest.labels_path,
    )
    if drift and not allow_drift:
        listed = "\n".join(f"  {line}" for line in render_changes(drift))
        raise CliInputError(
            f"the frozen set of {spec.experiment_id} changed since the plan was frozen, "
            f"so {refused}:\n{listed}\nRestore those files, or pass {override}."
        )
    return drift


def _branch(args: argparse.Namespace, store: ArtifactStore) -> int:
    """Run each condition of an experiment from a regression artifact (#159).

    Live conditions go through ``runner.branch``. ``static_replay`` conditions
    reuse the ``replay --apply-control`` path and record its verdict as a batch
    of one. Every condition is checked before any of them runs.
    """
    from trace_harness.runner.batch import BUDGET_UNENFORCEABLE
    from trace_harness.runner.branch import (
        admit_before_any_run,
        already_recorded,
        calls_a_provider,
        experiment_guard,
        load_artifact,
        recorded_cassettes,
        replacement_seeds,
        replay_batch,
        run_branch,
        validate_condition,
    )
    from trace_harness.runner.experiment import ConditionKind, ExperimentSpec

    artifact_path, spec_path = Path(args.artifact_path), Path(args.experiment)
    for path, what in ((artifact_path, "regression artifact"), (spec_path, "experiment plan")):
        if not path.is_file():
            raise CliInputError(f"{what} not found: {path}")
    spec = ExperimentSpec.model_validate(json.loads(spec_path.read_text(encoding="utf-8")))
    conditions = [c for c in spec.conditions if args.condition in (None, c.name)]
    if not conditions:
        declared = sorted(c.name for c in spec.conditions)
        raise CliInputError(f"condition {args.condition!r} is not declared; declared: {declared}")
    artifact = load_artifact(artifact_path)
    for condition in conditions:
        validate_condition(artifact, condition)
    replacement_seeds(spec)
    # Recording never overwrites a cassette. A condition branched before would
    # end its seeds as setup errors, so it is refused before anything runs (#200).
    for condition in conditions:
        existing = recorded_cassettes(artifact, spec, condition)
        if existing:
            raise CliInputError(already_recorded(condition, existing))
    # The same check record runs, made before any spend: a sweep on a changed
    # evaluator would be refused at record after its money was gone.
    drift = _frozen_set_drift(
        spec,
        spec_path,
        allow_drift=args.allow_drift,
        refused="branching is refused before any run",
        override="--allow-drift to run anyway (record will need it too)",
    )
    if drift:
        _print("frozen set:", f"DRIFTED, {len(drift)} file(s), running with --allow-drift")
    # One guard for the whole invocation, from the plan's cap (#196). It starts
    # from what earlier runs of this experiment spent, batched or not, and
    # stopped when an earlier stop left the cap unenforceable, so branching one
    # condition at a time cannot multiply the cap (#200). Asking it about every
    # live condition first means a cap that cannot hold stops every live run
    # before the first one starts.
    guard, earlier = experiment_guard(store, spec)
    if guard.spent_usd:
        _print("budget:", f"${guard.spent_usd:.6f} already spent by earlier runs of the plan")
    if guard.stop_reason is not None:
        _print("budget:", f"stopped before this invocation, {earlier.detail}")
    admit_before_any_run(guard, conditions)

    pairs: list[str] = []
    for condition in conditions:
        print(f"\nBranch condition: {condition.name} ({condition.kind.value})")
        if condition.kind is ConditionKind.STATIC_REPLAY:
            started_at = utc_now()
            report = _replay_with_report(
                artifact_path,
                store,
                apply_control=bool(condition.control_ids),
                control_ids=condition.control_ids or None,
            )
            summary = replay_batch(report, spec, condition, artifact_path, store, started_at)
        else:
            outcome = run_branch(artifact_path, spec, condition, store, guard=guard)
            if outcome.summary is None:
                print(f"  skipped: {outcome.skipped}")
                continue
            summary = outcome.summary
        for entry in summary.entries:
            divergence = (
                ""
                if entry.diverged is None
                else f", diverged={entry.diverged} "
                f"(first at step {entry.first_post_fork_divergence_step})"
            )
            _print(
                f"seed {entry.seed}:" if entry.seed is not None else "run:",
                f"{entry.run_id} {entry.status} {entry.verdict}, "
                f"post_block_outcome={entry.post_block_outcome}{divergence}",
            )
        budget = summary.budget
        if budget is not None and budget.stop_reason is not None:
            _print("stopped:", f"{budget.stop_reason}; {budget.detail}")
            _print("not run:", f"{len(budget.not_run)} seed(s)")
        _print("batch:", str(store.batch_summary_path(summary.batch_id)))
        pairs.append(f"--condition {condition.name}={summary.batch_id}")

    print()
    _print(
        "budget:",
        f"${guard.spent_usd:.6f} of ${guard.max_cost_usd:.6f} spent on live runs of the plan",
    )
    if guard.stop_reason is not None:
        _print("stopped:", f"{guard.stop_reason}; {guard.detail}")
    # Exits as run-suite does: a cap the harness cannot enforce is a
    # configuration problem, and an exhausted cap is a recorded early stop. An
    # invocation with no live condition never asked the guard, so a stop
    # carried over from earlier runs does not fail it.
    if guard.stop_reason == BUDGET_UNENFORCEABLE and any(map(calls_a_provider, conditions)):
        return 2
    if pairs:
        print("\nRecord with:")
        print(f"  trace-harness experiment record {spec_path} " + " ".join(pairs))
    return 0


def _list_experiments(store: ArtifactStore) -> int:
    """One line per experiment, replacing any hand-kept spreadsheet of them."""
    reader = RunReader(store)
    specs = reader.list_experiments()
    if not specs:
        print(f"no experiments found in {store.runs_dir}")
        return 0
    for spec in specs:
        _, result = reader.get_experiment(spec.experiment_id)
        decision = (
            f"{result.decision.value}/{result.decided_by.value}" if result else "not recorded"
        )
        conditions = ", ".join(c.name for c in spec.conditions)
        print(f"{spec.experiment_id}  {decision}  [{conditions}]  {spec.hypothesis[:60]}")
    print(f"\n{len(specs)} experiment(s) in {store.runs_dir}")
    return 0


def _list_runs(store: ArtifactStore, batch_id: str | None = None) -> None:
    """Print a one-line summary per run, newest last (chronological)."""
    reader = RunReader(store)
    summaries = reader.list_runs_for_batch(batch_id) if batch_id else reader.list_runs()
    where = f"batch {batch_id}" if batch_id else str(store.runs_dir)
    if not summaries:
        print(f"no runs found in {where}")
        return
    for s in summaries:
        detail = f"{s.status} ({s.termination_reason}) · {s.steps_taken} steps · {s.task_id}"
        if s.verdict is not None:
            detail += f" · {s.verdict.upper()}"
        elif s.verifier_passed is not None:
            detail += f" · {'PASS' if s.verifier_passed else 'FAIL'}"
        if s.batch_id:
            detail += f" · batch={s.batch_id}"
        _print(s.run_id, detail)
    print(f"\n{len(summaries)} run(s) in {where}")


def _event_summary(event: TraceEvent) -> str:  # noqa: PLR0911
    """One-line payload summary for a trace event, used by inspect."""
    p = event.typed_payload
    if p is None:
        return ""
    match event.event_type:
        case TraceEventType.RUN_STARTED:
            return f"task={p.task_id} provider={p.provider} model={p.model}"
        case TraceEventType.TASK_LOADED:
            return f"task_id={p.task.get('task_id', '?')}"
        case TraceEventType.STATE_SNAPSHOT:
            return f"phase={p.phase}"
        case TraceEventType.MODEL_PROMPT:
            return f"transcript_len={p.transcript_length} new_msgs={len(p.new_messages)}"
        case TraceEventType.MODEL_RESPONSE:
            return "raw=<present>" if p.raw else "raw=<none>"
        case TraceEventType.MODEL_ACTION:
            if p.tool_call:
                return f"kind={p.kind} tool={p.tool_call.get('tool_name', '?')}"
            if p.final_answer:
                ans = p.final_answer[:50] + ("…" if len(p.final_answer) > 50 else "")
                return f"kind={p.kind} answer={ans!r}"
            return f"kind={p.kind}"
        case TraceEventType.TOOL_CALL_REQUESTED:
            return f"tool={p.tool_name} args={list(p.arguments.keys())}"
        case TraceEventType.TOOL_CALL_VALIDATED:
            status = "valid" if p.valid else f"INVALID: {p.error or ''}"
            return f"tool={p.tool_name} {status}"
        case TraceEventType.TOOL_CALL_EXECUTED:
            parts = [f"tool={p.tool_name}", f"status={p.status}"]
            if p.side_effect:
                parts.append(f"side_effect={p.side_effect}")
            if p.error:
                parts.append(f"error={p.error}")
            return " ".join(parts)
        case TraceEventType.RETRIEVAL_RESULT:
            q = f"query={p.query[:40]!r} " if p.query else ""
            return f"{q}results={p.result_count}"
        case TraceEventType.TOOL_OBSERVATION:
            parts = [f"tool={p.tool_name}", f"status={p.status}"]
            if p.error:
                parts.append(f"error={p.error}")
            return " ".join(parts)
        case TraceEventType.FINAL_ANSWER:
            ans = p.final_answer[:60] + ("…" if len(p.final_answer) > 60 else "")
            return repr(ans)
        case TraceEventType.RUN_FINISHED:
            return f"status={p.status} termination={p.termination_reason} steps={p.steps_taken}"
        case TraceEventType.ERROR:
            err = p.error[:60] + ("…" if len(p.error) > 60 else "")
            return f"kind={p.kind} error={err}"
        case _:
            return ""


def _print_event(event: TraceEvent, children: dict[str, list[TraceEvent]], indent: int) -> None:
    prefix = "    " * indent
    etype = event.event_type.ljust(28)
    print(f"{prefix}  {etype} {_event_summary(event)}")
    for child in children.get(event.event_id, []):
        _print_event(child, children, indent + 1)


def _print_repair_validation(store: ArtifactStore, run_id: str) -> bool:
    """Summarize repair_validation.json when a validation session produced one."""
    if not store.exists(run_id, names.REPAIR_VALIDATION):
        return False
    try:
        validation = RepairValidation.model_validate(
            store.read_json(run_id, names.REPAIR_VALIDATION)
        )
    except FileNotFoundError:
        return False
    rollup = validation.rollup
    print(f"\nControl validation for {run_id} ({validation.controls_source}):")
    for control in validation.controls:
        detail = f" — {control.reason}" if control.reason else ""
        print(f"  {control.verdict.value:28} {control.control}{detail}")
    print(
        f"  rollup: {rollup.accepted} accepted, {rollup.rejected} rejected, "
        f"{rollup.skipped} skipped"
    )
    return True


def _inspect_run(run_dir: Path, step_filter: int | None, as_json: bool) -> None:
    """Print a human-readable timeline of a run's trace events."""
    store, run_id = ArtifactStore.for_run_path(run_dir)

    if not as_json and step_filter is None:
        has_validation = _print_repair_validation(store, run_id)
        if has_validation and not store.trace_path(run_id).exists():
            return

    events = store.read_trace(run_id)

    if step_filter is not None:
        events = [e for e in events if e.step_id == step_filter]

    if as_json:
        print(json.dumps([e.model_dump(mode="json") for e in events], indent=2))
        return

    if not events:
        if step_filter is not None:
            print(f"no events for step {step_filter} in {run_id}")
        else:
            print(f"no events in trace for {run_id}")
        return

    # child events (those with a parent) are printed recursively under their parent
    child_event_ids: set[str] = {e.event_id for e in events if e.parent_event_id}
    children: dict[str, list[TraceEvent]] = {}
    for e in events:
        if e.parent_event_id:
            children.setdefault(e.parent_event_id, []).append(e)

    print(f"\nRun:  {run_id}")
    if store.exists(run_id, names.RUN_RESULT):
        run_result = store.read_json(run_id, names.RUN_RESULT)
        print(
            f"Task: {run_result.get('task_id')} · status: {run_result.get('status')}"
            f" · {run_result.get('steps_taken')} steps"
        )
    else:
        print("Result: unavailable (partial run)")

    sentinel = object()
    current_step: object = sentinel
    for e in events:
        if e.event_id in child_event_ids:
            continue
        if e.step_id != current_step:
            current_step = e.step_id
            label = "run-level" if current_step is None else f"step {current_step}"
            bar = "─" * max(0, 52 - len(label))
            print(f"\n── {label} {bar}")
        _print_event(e, children, indent=0)

    print(f"\n{len(events)} event(s)")


def _run_suite(args: argparse.Namespace, store: ArtifactStore) -> int:
    """Run a task suite (batch) and print + persist a batch summary."""
    from trace_harness.runner.batch import BUDGET_UNENFORCEABLE, BatchRunner, summary_path
    from trace_harness.runner.suite import load_suite

    suite = load_suite(Path(args.suite_path))
    cells = len(suite.tasks) * len(suite.agent_configs)
    print(
        f"\nRunning suite '{suite.suite_id}': {len(suite.tasks)} task(s) x "
        f"{len(suite.agent_configs)} agent config(s) = {cells} run(s)"
    )

    summary = BatchRunner(store, control_library=args.control_library).run(suite)

    print(f"\nBatch {summary.batch_id} complete:")
    for e in summary.entries:
        verdict = (
            "PASS" if e.verifier_passed is True else "FAIL" if e.verifier_passed is False else "-"
        )
        rid = e.run_id or "(no run)"
        detail = f"{e.status} · verdict={verdict} · {rid}"
        if e.error:
            detail += f" · {e.error}"
        _print(f"{e.agent_label} / {e.task_id}", detail)

    agg = summary.aggregates
    print()
    _print("total runs:", str(agg.total))
    _print("completed:", str(agg.completed))
    _print("terminated:", str(agg.terminated))
    _print("passed / failed:", f"{agg.verifier_passed} / {agg.verifier_failed}")
    _print("errored:", str(agg.errored))
    _print("known cost:", f"${agg.known_cost_usd:.6f} ({agg.cost_recorded}/{agg.total} runs)")
    budget = summary.budget
    if budget is not None:
        _print(
            "budget:",
            f"${budget.spent_usd:.6f} of ${budget.max_cost_usd:.6f} spent on live runs",
        )
        if budget.stop_reason is not None:
            _print("stopped:", f"{budget.stop_reason}; {budget.detail}")
            _print("not run:", f"{len(budget.not_run)} cell(s)")
    _print("pass_rate:", "n/a" if agg.pass_rate is None else f"{agg.pass_rate:.0%}")
    _print("summary:", str(summary_path(store.runs_dir, summary.batch_id)))

    if getattr(args, "report", False):
        _write_and_print_suite_report(store, summary.batch_id, print_full=False)

    # A cap the harness cannot enforce is a configuration problem, like a bad path.
    if budget is not None and budget.stop_reason == BUDGET_UNENFORCEABLE:
        return 2
    stopped_early = budget is not None and bool(budget.not_run)
    if args.fail_on_verifier and (
        agg.verifier_failed > 0 or agg.terminated > 0 or agg.errored > 0 or stopped_early
    ):
        return 1
    if agg.errored > 0 and any(config.cassette is not None for config in suite.agent_configs):
        return 2
    return 0


def _collect_regressions(args: argparse.Namespace, store: ArtifactStore) -> int:
    from trace_harness.runner.collector import SUMMARY_NAME, collect_regressions
    from trace_harness.runner.frozen_set import render_changes

    summary = collect_regressions(
        args.path, store, suite_path=args.suite, experiments_path=args.experiments
    )
    print("\nRegression collection:")
    for entry in summary.entries:
        label = entry.test_name or entry.artifact_path
        if entry.error is not None:
            detail = f"ERROR: {entry.error}"
        elif entry.blocks_release is False:
            detail = "SKIP: does not block release"
        else:
            baseline = "reproduced" if entry.baseline.reproduced else "NOT REPRODUCED"
            detail = f"{baseline}; controls {entry.control_status} ({entry.replay_mode})"
        _print(label, detail)
    for experiment in summary.experiments:
        detail = {
            "matches": "frozen set matches",
            "drifted": f"WARNING frozen set drifted, {len(experiment.changes)} file(s), not gating",
            "not_recorded": "frozen set not recorded in the plan",
        }[experiment.status]
        _print(experiment.experiment_id, detail)
        for line in render_changes(experiment.changes[:10]):
            print(f"    {line}")
        if len(experiment.changes) > 10:
            print(f"    and {len(experiment.changes) - 10} more in the summary")
    for test_name, sibling in summary.siblings_failed:
        _print("sibling failed:", f"{test_name} / {sibling}")
    for error in summary.errors:
        _print("error:", error)
    _print("artifacts found:", str(summary.artifacts_found))
    _print("release-blocking:", str(summary.blocking))
    _print("reproduced:", str(summary.reproduced))
    _print("siblings passed:", str(summary.siblings_passed))
    _print("controls confirmed:", str(summary.controls_confirmed))
    _print("controls advisory:", str(summary.controls_advisory))
    _print("controls failed:", str(len(summary.controls_failed)))
    _print("malformed:", str(len(summary.malformed)))
    if args.experiments is not None:
        _print("experiments checked:", str(len(summary.experiments)))
        _print("experiments drifted:", f"{len(summary.experiments_drifted)} (warning only)")
    _print("duration:", f"{summary.duration_s:.3f}s")
    _print("summary:", str(store.runs_dir / SUMMARY_NAME))
    _print("gate:", "PASS" if summary.exit_code == 0 else f"FAIL (exit {summary.exit_code})")
    if args.append_history is not None:
        _append_metrics_history(args, store)
    return summary.exit_code


def _append_metrics_history(args: argparse.Namespace, store: ArtifactStore) -> None:
    """Record the three trend measures for this commit (#207).

    Appended after the gate has printed its verdict and it never changes the
    exit code. A history file is a record of what main looked like, so a write
    problem here must not turn a passing gate into a failing one.
    """
    from trace_harness.metrics.history import append_snapshot, build_snapshot

    path = Path(args.append_history)
    commit = args.commit or _current_commit()
    if not commit:
        _print("history:", "skipped, no commit given and git did not report one")
        return
    try:
        snapshot = build_snapshot(Path(args.history_root), commit=commit, exclude=[store.runs_dir])
        written = append_snapshot(path, snapshot)
    except OSError as exc:
        _print("history:", f"skipped, {exc}")
        return
    coverage = snapshot.coverage.accepted_over_prescribed
    blocking = snapshot.over_blocking.rate
    print("\nMetrics history:")
    _print("commit:", snapshot.commit)
    _print("coverage:", f"{coverage.numerator}/{coverage.denominator} accepted of prescribed")
    _print("over-blocking:", f"{blocking.numerator}/{blocking.denominator} siblings failed")
    _print(
        "cost of learning:",
        f"{snapshot.cost_of_learning.irreversible_actions} irreversible actions, "
        f"${snapshot.cost_of_learning.money_moved_usd:.2f} moved",
    )
    _print("history:", str(path) if written else f"{path} already records {snapshot.commit}")


def _current_commit() -> str | None:
    """The commit being recorded, when the workflow did not pass one."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None


def _write_and_print_suite_report(store: ArtifactStore, batch_id: str, *, print_full: bool) -> int:
    """Build the per-batch report from on-disk artifacts, persist it, print it.

    ``print_full`` prints the whole markdown rendering (``report-suite``); the
    ``run-suite --report`` path prints only the paths + the coverage-gap lists
    so it does not bury the batch summary it just showed.
    """
    from trace_harness.runner.batch import BatchSummary
    from trace_harness.runner.report import build_suite_report, render_suite_report_markdown

    summary = BatchSummary.model_validate(store.read_batch_summary(batch_id))
    report = build_suite_report(summary, store)
    markdown = render_suite_report_markdown(report)
    json_path = store.write_suite_report(batch_id, report, markdown=markdown)

    if print_full:
        print(markdown)
    print()
    _print("suite report:", str(json_path))
    _print("suite report (md):", str(store.suite_report_md_path(batch_id)))
    _print("rows / failing:", f"{report.total_rows} / {report.failing_rows}")
    _print(
        "modes claimed, never observed:",
        ", ".join(report.coverage.claimed_never_observed) or "—",
    )
    _print(
        "categories observed, never claimed:",
        ", ".join(report.coverage.observed_never_claimed) or "—",
    )
    for warning in report.warnings:
        print(f"  ⚠ {warning}")
    return 0


def _report_suite(store: ArtifactStore, batch_id: str) -> int:
    """`report-suite <batch_id>`: (re)generate and persist the batch's report."""
    return _write_and_print_suite_report(store, batch_id, print_full=True)


def _force_utf8_stdio() -> None:
    """Make stdout/stderr UTF-8 so verifier glyphs (✗, ⚠) never crash the CLI.

    On a default Windows console stdout is cp1252, and printing the verdict
    lines raises UnicodeEncodeError mid-pipeline — aborting before attribution
    and the failure bundle run. Reconfiguring to UTF-8 (best-effort; older
    streams without ``reconfigure`` are left as-is) keeps the demo runnable on
    any platform.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):  # pragma: no cover - platform dependent
                pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    load_env_file()  # opt-in convenience; real env vars always win
    harness_config = HarnessConfig.from_env()
    # Unknown TRACE_LOG_LEVEL values fall back to INFO rather than crashing.
    logging.basicConfig(level=getattr(logging, harness_config.log_level.upper(), logging.INFO))

    # --runs-dir is accepted both before and after the subcommand (users
    # reliably append flags at the end). SUPPRESS keeps the subparser's
    # default from clobbering a value parsed by the main parser.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--runs-dir",
        default=argparse.SUPPRESS,
        help=f"runs directory (default: $TRACE_RUNS_DIR or {harness_config.runs_dir})",
    )

    parser = argparse.ArgumentParser(
        prog="trace-harness",
        parents=[common],
        description=(
            "TRACE agent reliability harness. Run scripted fixture agents, "
            "verify outcomes deterministically, attribute failures, and "
            "generate failure bundles."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser(
        "run-fixture", parents=[common], help="run a task with a model agent (default: fixture)"
    )
    p_run.add_argument("task_path", help="path to a task fixture JSON")
    p_run.add_argument("--script", default=None, help="override the fixture script path")
    p_run.add_argument("--max-steps", type=int, default=16)
    _add_provider_args(p_run)

    p_verify = sub.add_parser(
        "verify", parents=[common], help="run the task's verifiers on a finished run"
    )
    p_verify.add_argument("run_path", help="path to a runs/<run_id> directory")
    p_verify.add_argument(
        "--fail-on-verifier",
        action="store_true",
        help="exit 1 if the verifier fails (CI gate mode)",
    )

    p_attr = sub.add_parser(
        "attribute", parents=[common], help="run heuristic attribution on a verified failure"
    )
    p_attr.add_argument("run_path")

    p_bundle = sub.add_parser(
        "bundle", parents=[common], help="generate failure card/repair/regression artifacts"
    )
    p_bundle.add_argument("run_path")

    p_list = sub.add_parser(
        "list-runs", parents=[common], help="list stored runs with one-line summaries"
    )
    p_list.add_argument(
        "--batch",
        default=None,
        metavar="BATCH_ID",
        help="filter to runs from a specific batch",
    )

    p_inspect = sub.add_parser(
        "inspect",
        parents=[common],
        help="print a human-readable timeline of events in a run's trace",
    )
    p_inspect.add_argument("run_path", help="run directory path or bare run id")
    p_inspect.add_argument(
        "--step",
        type=int,
        default=None,
        metavar="N",
        help="show only events for agent decision step N",
    )
    p_inspect.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="emit raw JSON array of events (for piping)",
    )

    p_replay = sub.add_parser(
        "replay",
        parents=[common],
        help="replay a regression artifact and assert the gate conditions hold",
    )
    p_replay.add_argument(
        "artifact_path",
        help="path to a regression_artifact.json produced by the bundle stage",
    )
    p_replay.add_argument(
        "--apply-control",
        action="store_true",
        help=(
            "install the reference controls (trace_harness.environment.controls) "
            "before replaying, to demonstrate a repair control flipping the gate"
        ),
    )
    p_replay.add_argument(
        "--fail-on-rejected",
        action="store_true",
        help="exit 1 if any control is rejected (CI gate mode; requires --apply-control)",
    )
    p_replay.add_argument(
        "--control",
        action="append",
        dest="control_ids",
        metavar="CONTROL_ID",
        help=(
            "with --apply-control: install only this control id (repeatable); "
            "default is every reference control"
        ),
    )
    p_replay.add_argument(
        "--control-library",
        default=None,
        metavar="PATH",
        help="replay with active library controls",
    )
    p_replay.add_argument(
        "--commit",
        action="store_true",
        help="retain accepted controls after library replay (requires --apply-control)",
    )

    p_controls = sub.add_parser("controls", parents=[common], help="manage the control library")
    control_commands = p_controls.add_subparsers(dest="control_command", required=True)
    p_rollback = control_commands.add_parser(
        "rollback", help="deactivate a control, preserving history"
    )
    p_rollback.add_argument("control_id")
    p_rollback.add_argument("--reason", required=True)
    p_rollback.add_argument(
        "--control-library", default=str(DEFAULT_CONTROL_LIBRARY), metavar="PATH"
    )

    p_pipe = sub.add_parser(
        "run-pipeline",
        parents=[common],
        help="run-fixture + verify + (on failure) attribute + bundle",
    )
    p_pipe.add_argument("task_path")
    p_pipe.add_argument("--script", default=None)
    p_pipe.add_argument("--max-steps", type=int, default=16)
    p_pipe.add_argument("--fail-on-verifier", action="store_true")
    _add_provider_args(p_pipe)

    p_validate = sub.add_parser(
        "validate-fixtures",
        parents=[common],
        help="authoring-validate every task fixture under a directory",
    )
    p_validate.add_argument(
        "path", nargs="?", default="fixtures/tasks", help="directory to validate"
    )

    p_exp = sub.add_parser(
        "experiment",
        parents=[common],
        help="record which batch answered which condition of an experiment",
    )
    exp_sub = p_exp.add_subparsers(dest="experiment_command", required=True)
    p_exp_record = exp_sub.add_parser("record", parents=[common], help="write the result file")
    p_exp_record.add_argument("experiment_path", help="path to the experiment plan JSON")
    p_exp_record.add_argument(
        "--condition",
        action="append",
        metavar="NAME=BATCH_ID",
        help="map a declared condition to the batch that answered it (repeatable)",
    )
    p_exp_record.add_argument(
        "--decision", default="baseline", choices=["baseline", "keep", "discard", "review"]
    )
    p_exp_record.add_argument("--decided-by", default="human", choices=["human", "policy"])
    p_exp_record.add_argument(
        "--allow-drift",
        action="store_true",
        help="record even if the frozen set changed; marks the result drifted, decision review",
    )
    p_exp_freeze = exp_sub.add_parser(
        "freeze", parents=[common], help="hash the frozen set into a plan before anything runs"
    )
    p_exp_freeze.add_argument("experiment_path", help="path to the experiment plan JSON")

    sub.add_parser(
        "list-experiments", parents=[common], help="list recorded experiments, oldest first"
    )

    p_branch = sub.add_parser(
        "branch",
        parents=[common],
        help="continue a recorded run from each experiment condition's start step",
    )
    p_branch.add_argument("artifact_path", help="path to a regression_artifact.json")
    p_branch.add_argument("--experiment", required=True, help="path to the experiment plan JSON")
    p_branch.add_argument(
        "--condition", default=None, metavar="NAME", help="run only this declared condition"
    )
    p_branch.add_argument(
        "--allow-drift",
        action="store_true",
        help="run even if the plan's frozen set changed; record will need the flag too",
    )

    p_suite = sub.add_parser(
        "run-suite",
        parents=[common],
        help="run a task suite (batch) across agent configs and write a batch summary",
    )
    p_suite.add_argument("suite_path", help="path to a suite manifest JSON")
    p_suite.add_argument("--control-library", default=None, metavar="PATH")
    p_suite.add_argument(
        "--fail-on-verifier",
        action="store_true",
        help=(
            "exit 1 if any run failed verification or errored, or the budget stopped the "
            "batch early (CI gate mode)"
        ),
    )
    p_suite.add_argument(
        "--report",
        action="store_true",
        help="also write suite_report.json/.md (checks fired, failure categories, coverage gaps)",
    )

    p_report = sub.add_parser(
        "report-suite",
        parents=[common],
        help="roll a finished batch's artifacts into a per-batch check/category/coverage report",
    )
    p_report.add_argument(
        "batch_id", help="batch id (from run-suite's output or `list-runs --batch`)"
    )

    p_collect = sub.add_parser(
        "collect-regressions",
        parents=[common],
        help="replay release-blocking artifacts as a CI gate",
    )
    p_collect.add_argument("path", help="artifact file or directory to search recursively")
    p_collect.add_argument(
        "--suite", default=None, help="also generate artifacts from this offline fixture suite"
    )
    p_collect.add_argument(
        "--experiments",
        default=None,
        metavar="DIR",
        help="also check each retained experiment's frozen set; drift warns without gating",
    )
    p_collect.add_argument(
        "--append-history",
        nargs="?",
        const=str(DEFAULT_HISTORY_PATH),
        default=None,
        metavar="PATH",
        help=f"append a metrics snapshot for this commit (default {DEFAULT_HISTORY_PATH})",
    )
    p_collect.add_argument(
        "--commit", default=None, help="commit to record; defaults to git rev-parse HEAD"
    )
    p_collect.add_argument(
        "--history-root",
        default=".",
        help="tree the snapshot reads artifacts from (default the working tree)",
    )

    args = parser.parse_args(argv)
    runs_dir_arg = getattr(args, "runs_dir", None)
    runs_dir = Path(runs_dir_arg) if runs_dir_arg else harness_config.runs_dir
    store = ArtifactStore(runs_dir)

    try:
        return _dispatch(args, store)
    except ProviderNotConfiguredError as exc:
        # A missing key or SDK is a setup problem, and the adapter's message
        # already says exactly what to do about it. Burying that under a
        # traceback helps nobody.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (FileNotFoundError, KeyError, ValueError) as exc:
        # Expected input problems — a mistyped run path, a malformed fixture
        # (TaskLoadError and pydantic ValidationError are ValueErrors), an
        # unknown verifier id (KeyError), or a pipeline stage run out of
        # order — get a clean message instead of a traceback. Genuine bugs
        # are none of these types and still raise loudly.
        message = str(exc).strip("'") if isinstance(exc, KeyError) else str(exc)
        print(f"error: {message}", file=sys.stderr)
        return 2


def _dispatch(args: argparse.Namespace, store: ArtifactStore) -> int:
    if args.command == "run-fixture":
        _run_fixture(args, store)
        return 0
    if args.command == "list-runs":
        _list_runs(store, batch_id=getattr(args, "batch", None))
        return 0
    if args.command == "inspect":
        _inspect_run(_resolve_run_dir(args.run_path, store.runs_dir), args.step, args.as_json)
        return 0
    if args.command == "verify":
        merged, run_completed = _verify(_resolve_run_dir(args.run_path, store.runs_dir))
        return 1 if (args.fail_on_verifier and not (merged.passed and run_completed)) else 0
    if args.command == "attribute":
        _attribute(_resolve_run_dir(args.run_path, store.runs_dir))
        return 0
    if args.command == "bundle":
        _bundle(_resolve_run_dir(args.run_path, store.runs_dir))
        return 0
    if args.command == "replay":
        return _replay(
            Path(args.artifact_path),
            store,
            apply_control=args.apply_control,
            control_ids=args.control_ids,
            fail_on_rejected=args.fail_on_rejected,
            control_library=Path(args.control_library) if args.control_library else None,
            commit=args.commit,
        )
    if args.command == "controls":
        rollback_control(args.control_library, args.control_id, args.reason)
        print(f"Rolled back {args.control_id}: {args.reason.strip()}")
        return 0
    if args.command == "run-pipeline":
        result = _run_fixture(args, store)
        run_dir = store.run_dir(result.run_id)
        merged, run_completed = _verify(run_dir)
        if not merged.passed:
            _attribute(run_dir)
            _bundle(run_dir)
        else:
            print("\nVerifier passed: no attribution or failure bundle needed.")
        print(f"\nPipeline complete. Inspect artifacts in: {run_dir}")
        return 1 if (args.fail_on_verifier and not (merged.passed and run_completed)) else 0
    if args.command == "validate-fixtures":
        return _validate_fixtures(args)
    if args.command == "experiment":
        if args.experiment_command == "freeze":
            return _experiment_freeze(args)
        return _experiment_record(args, store)
    if args.command == "list-experiments":
        return _list_experiments(store)
    if args.command == "branch":
        return _branch(args, store)
    if args.command == "run-suite":
        return _run_suite(args, store)
    if args.command == "collect-regressions":
        return _collect_regressions(args, store)
    if args.command == "report-suite":
        return _report_suite(store, args.batch_id)
    raise AssertionError(f"unhandled command {args.command}")  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
