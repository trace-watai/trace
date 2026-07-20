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
import logging
import sys
from pathlib import Path

from trace_harness.config import HarnessConfig, load_env_file
from trace_harness.environment.support_env import SupportEnvironment
from trace_harness.models import create_model_adapter
from trace_harness.run_reader import RunReader
from trace_harness.runner.agent_runner import AgentRunner
from trace_harness.runner.config import RunConfig
from trace_harness.runner.result import RunResult, RunStatus
from trace_harness.tasks.loader import load_docs_for_task, load_task
from trace_harness.tasks.schemas import TaskSpec
from trace_harness.tracing import artifact_store as names
from trace_harness.tracing.artifact_store import ArtifactStore
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


def _run_fixture(args: argparse.Namespace, store: ArtifactStore) -> RunResult:
    task_path = Path(args.task_path).resolve()
    task = load_task(task_path)
    docs = load_docs_for_task(task, task_path)

    environment = SupportEnvironment.from_task(task, docs=docs)
    metadata: dict[str, str] = {"task_fixture_path": _repo_relative(task_path)}

    # The fixture provider replays a script; real providers (gemini) drive the
    # agent live and need no script — only the fixture path is required.
    if args.provider == "fixture":
        script_path = _resolve_script_path(task, task_path, args.script)
        adapter = create_model_adapter("fixture", script_path=script_path)
        model = f"scripted:{script_path.stem}"
        metadata["fixture_script_path"] = _repo_relative(script_path)
    else:
        adapter = create_model_adapter(args.provider, model=args.model)
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


def _list_runs(store: ArtifactStore) -> None:
    """Print a one-line summary per run, newest last (chronological)."""
    summaries = RunReader(store).list_runs()
    if not summaries:
        print(f"no runs found in {store.runs_dir}")
        return
    for s in summaries:
        detail = f"{s.status} ({s.termination_reason}) · {s.steps_taken} steps · {s.task_id}"
        _print(s.run_id, detail)
    print(f"\n{len(summaries)} run(s) in {store.runs_dir}")


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
    _print("passed / failed:", f"{agg.verifier_passed} / {agg.verifier_failed}")
    _print("errored:", str(agg.errored))
    _print("pass_rate:", "n/a" if agg.pass_rate is None else f"{agg.pass_rate:.0%}")
    _print("summary:", str(summary_path(store.runs_dir, summary.batch_id)))

    if args.fail_on_verifier and (agg.verifier_failed > 0 or agg.errored > 0):
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
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

    sub.add_parser("list-runs", parents=[common], help="list stored runs with one-line summaries")

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
        _list_runs(store)
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
