"""trace-harness CLI: run fixtures and drive the failure pipeline.

Commands (each is one pipeline stage; ``run-pipeline`` chains them):

    trace-harness run-fixture  fixtures/tasks/refund_policy_failure.json
    trace-harness verify       runs/<run_id>
    trace-harness attribute    runs/<run_id>
    trace-harness bundle       runs/<run_id>
    trace-harness run-pipeline fixtures/tasks/refund_policy_failure.json
    trace-harness run-suite    fixtures/suites/refund_v0.json

``run-suite`` runs many tasks across agent configs in one batch, isolating
per-run failures and writing a batch summary for dashboard metrics.

Stages communicate only through run artifacts on disk — ``verify`` reads
exactly what ``run-fixture`` wrote — so any stage can be re-run later, and
the dashboard/API see the same data the pipeline used.

Exit codes: 0 success; 1 verifier failed AND --fail-on-verifier was passed
(CI gate mode); 2 usage or input errors (argparse errors, bad paths,
malformed fixtures, missing artifacts). Without the flag a verified
failure exits 0 — finding failures is this tool succeeding.

argparse over typer: subcommands this simple don't justify a dependency.
Revisit if the CLI grows rich help/completions needs.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from trace_harness.config import HarnessConfig, load_env_file
from trace_harness.environment.guardrails import unauthorized_cash_refund_guardrail
from trace_harness.environment.state import SupportState
from trace_harness.environment.support_env import SupportEnvironment
from trace_harness.environment.tools import ToolResult
from trace_harness.models import create_model_adapter
from trace_harness.models.base import ToolCall
from trace_harness.models.fixture import FixtureModelAdapter, FixtureScript
from trace_harness.regression.replay import (
    describe_action_drift,
    describe_state_drift,
    pinned_initial_state,
)
from trace_harness.regression.replay import pinned_script as build_pinned_script
from trace_harness.regression.schemas import RegressionArtifact
from trace_harness.run_reader import RunReader
from trace_harness.runner.agent_runner import AgentRunner
from trace_harness.runner.config import RunConfig
from trace_harness.runner.result import RunResult, RunStatus
from trace_harness.tasks.loader import load_docs_for_task, load_task
from trace_harness.tasks.schemas import TaskSpec
from trace_harness.tracing import artifact_store as names
from trace_harness.tracing.artifact_store import ArtifactStore
from trace_harness.tracing.events import TraceEvent, TraceEventType
from trace_harness.verifiers.base import VerifierInput, VerifierResult, merge_verifier_results
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
        "--provider",
        default="fixture",
        help="model provider: 'fixture' (scripted, default) or 'gemini'",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="model name for real providers (e.g. gemini-2.0-flash); ignored by fixture",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="max wall-clock seconds for the whole run (default: 120)",
    )


def _run_fixture(
    args: argparse.Namespace,
    store: ArtifactStore,
    extra_hooks: list[Callable[[ToolCall, SupportState], ToolResult | None]] | None = None,
    pinned_initial_state: dict[str, Any] | None = None,
    pinned_script: FixtureScript | None = None,
) -> RunResult:
    task_path = Path(args.task_path).resolve()
    task = load_task(task_path)
    metadata: dict[str, str] = {"task_fixture_path": _repo_relative(task_path)}

    if pinned_initial_state is None:
        docs = load_docs_for_task(task, task_path)
    else:
        # Regression replay: the artifact's pinned snapshot is the world, docs
        # included, so the live docs fixture is deliberately never read.
        task = task.model_copy(update={"initial_state": pinned_initial_state})
        docs = None
        metadata["replay_pinned_state"] = "true"

    environment = SupportEnvironment.from_task(task, docs=docs)
    for hook in extra_hooks or []:
        environment.register_pre_execute_hook(hook)

    # The fixture provider replays a script; real providers (gemini) drive the
    # agent live and need no script — only the fixture path is required.
    if args.provider == "fixture" and pinned_script is not None:
        # Replaying pinned actions: the script file is not consulted at all, so
        # editing it cannot change what an existing regression asserts.
        adapter = FixtureModelAdapter(pinned_script)
        model = f"scripted:{pinned_script.script_id}"
        metadata["replay_pinned_script"] = "true"
    elif args.provider == "fixture":
        script_path = _resolve_script_path(task, task_path, args.script)
        adapter = create_model_adapter("fixture", script_path=script_path)
        model = f"scripted:{script_path.stem}"
        metadata["fixture_script_path"] = _repo_relative(script_path)
    else:
        adapter = create_model_adapter(
            args.provider,
            model=args.model,
            timeout_seconds=args.timeout,
        )
        model = args.model

    config = RunConfig(
        task_id=task.task_id,
        provider=args.provider,
        model=model,
        max_steps=args.max_steps,
        timeout_seconds=args.timeout,
        metadata=metadata,
    )
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
    print(f"\nNext: trace-harness verify {store.run_dir(result.run_id)}")
    return result


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
    store.write_json(run_id, names.VERIFIER_RESULT, merged)
    try:
        store.enrich_index_entry_with_verifier(run_id)
    except Exception:  # noqa: BLE001
        logger.exception(
            "run index verifier enrich failed for %s; verifier_result is the source of truth",
            run_id,
        )

    verdict = "PASS" if merged.passed else "FAIL"
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
    if not run_completed:
        print(
            f"  ⚠ run did not complete (status={run_result.status.value}, "
            f"termination={run_result.termination_reason.value}); a PASS only means "
            "no violations were recorded — the --fail-on-verifier gate treats "
            "incomplete runs as failures"
        )
    _print("written:", str(store.artifact_path(run_id, names.VERIFIER_RESULT)))
    return merged, run_completed


def _attribute(run_dir: Path) -> bool:
    """Returns True if an attribution was produced (verifier had failed)."""
    from trace_harness.attribution.heuristic import HeuristicAttributor

    store, run_id = ArtifactStore.for_run_path(run_dir)
    task = TaskSpec.model_validate(store.read_json(run_id, names.TASK_SPEC))
    trace = store.read_trace(run_id)
    verifier_result = VerifierResult.model_validate(store.read_json(run_id, names.VERIFIER_RESULT))
    if verifier_result.passed:
        print(f"\nVerifier passed for {run_id}; nothing to attribute.")
        return False

    attribution = HeuristicAttributor().attribute(task, trace, verifier_result)
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
    if verifier_result.passed:
        print(f"\nVerifier passed for {run_id}; no failure bundle to generate.")
        return False
    attribution = AttributionResult.model_validate(
        store.read_json(run_id, names.ATTRIBUTION_RESULT)
    )
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


def _replay(artifact_path: Path, store: ArtifactStore, *, apply_control: bool = False) -> int:
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

    With ``apply_control``: installs the reference guardrails (environment.
    guardrails) on the environment before every run in this replay, and
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

    Returns 0 if all assertions hold, 1 if the regression gate fires.
    """
    data = json.loads(artifact_path.read_text(encoding="utf-8"))
    artifact = RegressionArtifact.model_validate(data)
    pinned_state = pinned_initial_state(artifact)
    # The task fixture is a hard requirement (tool subset + verifier ids), so a
    # missing one is a usage error (exit 2), not something to replay around.
    task_path = Path(artifact.task_fixture).resolve()
    task = load_task(task_path)
    script = build_pinned_script(artifact, task.task_id)

    print(f"\nReplaying regression: {artifact.test_name}")
    _print("source_run_id:", artifact.source_run_id)
    _print("severity:", str(artifact.severity))
    _print("blocks_release:", str(artifact.blocks_release))
    _print("apply_control:", str(apply_control))
    _print("pinned inputs:", "state + docs" + (" + agent actions" if script else " (no actions)"))

    for note in _replay_drift_notes(artifact, task, task_path, pinned_state):
        print(f"  ⚠ fixture drift — {note}")

    hooks = [unauthorized_cash_refund_guardrail] if apply_control else []
    gate_failed = False

    def _fixture_args(task_path: str) -> argparse.Namespace:
        return argparse.Namespace(
            task_path=task_path,
            script=None,
            provider="fixture",
            model=None,
            max_steps=16,
            timeout=120.0,
        )

    # Step 1: re-run the pinned scenario; verifier must produce the expected failures.
    print(f"\n[1/2] Replaying pinned scenario (script from {artifact.task_fixture})")
    result = _run_fixture(
        _fixture_args(artifact.task_fixture),
        store,
        extra_hooks=hooks,
        pinned_initial_state=pinned_state,
        pinned_script=script,
    )
    run_dir = store.run_dir(result.run_id)
    merged, _ = _verify(run_dir)

    actual_ids = {c.check_id for c in merged.failed_checks}
    expected_ids = set(artifact.verifier_checks)

    if apply_control:
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
        print(f"\n[2/2] Running {len(artifact.positive_sibling_tests)} positive sibling(s)")
        for i, sibling in enumerate(artifact.positive_sibling_tests, 1):
            print(f"  [{i}] {sibling.test_name}: {sibling.task_fixture}")
            sib_result = _run_fixture(_fixture_args(sibling.task_fixture), store, extra_hooks=hooks)
            sib_dir = store.run_dir(sib_result.run_id)
            sib_merged, _ = _verify(sib_dir)
            if not sib_merged.passed:
                failed = sorted(c.check_id for c in sib_merged.failed_checks)
                print(f"      FAIL: sibling must pass but failed on {failed}")
                gate_failed = True
            else:
                print("      PASS")

    status = "FAIL — regression gate fired" if gate_failed else "PASS — regression gate clear"
    print(f"\nReplay result: {status}\n")
    return 1 if gate_failed else 0


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
        if s.verifier_passed is not None:
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


def _inspect_run(run_dir: Path, step_filter: int | None, as_json: bool) -> None:
    """Print a human-readable timeline of a run's trace events."""
    store, run_id = ArtifactStore.for_run_path(run_dir)
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
    from trace_harness.runner.batch import BatchRunner, summary_path
    from trace_harness.runner.suite import load_suite

    suite = load_suite(Path(args.suite_path))
    cells = len(suite.tasks) * len(suite.agent_configs)
    print(
        f"\nRunning suite '{suite.suite_id}': {len(suite.tasks)} task(s) x "
        f"{len(suite.agent_configs)} agent config(s) = {cells} run(s)"
    )

    summary = BatchRunner(store).run(suite)

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
    _print("pass_rate:", "n/a" if agg.pass_rate is None else f"{agg.pass_rate:.0%}")
    _print("summary:", str(summary_path(store.runs_dir, summary.batch_id)))

    if args.fail_on_verifier and (agg.verifier_failed > 0 or agg.terminated > 0 or agg.errored > 0):
        return 1
    return 0


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
            "install the reference guardrails (trace_harness.environment.guardrails) "
            "before replaying, to demonstrate a repair control flipping the gate"
        ),
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

    p_suite = sub.add_parser(
        "run-suite",
        parents=[common],
        help="run a task suite (batch) across agent configs and write a batch summary",
    )
    p_suite.add_argument("suite_path", help="path to a suite manifest JSON")
    p_suite.add_argument(
        "--fail-on-verifier",
        action="store_true",
        help="exit 1 if any run failed verification or errored (CI gate mode)",
    )

    args = parser.parse_args(argv)
    runs_dir_arg = getattr(args, "runs_dir", None)
    runs_dir = Path(runs_dir_arg) if runs_dir_arg else harness_config.runs_dir
    store = ArtifactStore(runs_dir)

    try:
        return _dispatch(args, store)
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
        return _replay(Path(args.artifact_path), store, apply_control=args.apply_control)
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
    if args.command == "run-suite":
        return _run_suite(args, store)
    raise AssertionError(f"unhandled command {args.command}")  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
