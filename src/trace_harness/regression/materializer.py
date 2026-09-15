"""Turn a verified failure into a pinned, rerunnable regression artifact.

What gets pinned and why
    - ``initial_state`` and ``pinned_docs`` come from the run's recorded
      initial state, not from the live fixture files — so the regression
      keeps testing the world the failure actually happened in, even if
      fixtures evolve later.
    - ``pinned_agent_actions`` comes from the trace's recorded MODEL_ACTION
      events, for the same reason applied to the other half of the inputs:
      the world is worthless as a pin if the agent's moves through it can
      still be edited out from under the regression. Taking them from the
      trace rather than re-reading the script file also means this works
      identically for a live model adapter, which has no script.
    - ``metadata["schema_versions"]`` records the shape of every input this
      artifact depends on, so a future reader can tell whether a replay
      mismatch is a real regression or just a schema that moved. There is
      deliberately no tool version: the tool registry has no version concept
      to record, so ``available_tools`` (the surface the run actually had) is
      recorded instead of inventing one.
    - ``verifier_checks`` are the check ids that failed; replaying the
      regression means asserting these checks (still) hold on a fixed agent.
    - ``positive_sibling_tests`` come from the task's
      ``metadata.positive_sibling_tasks`` so every blocking regression is
      paired with at least one scenario that must keep passing.

Replay
    ``trace-harness replay <regression_artifact.json>`` rebuilds the scenario
    from the pinned ``initial_state``/``pinned_docs`` above (not from the
    fixture's current contents), asserts the verifier produces the expected
    failed check IDs, then runs each positive sibling and asserts it passes.
    See ``cli._replay`` for the implementation and ``replay.py`` for how the
    pinned world is reconstructed.

    Note that ``replay_command`` is *not* that command: it is a plain
    ``run-pipeline`` on the originating fixture, which reflects whatever that
    fixture says today. It is a convenience for humans reproducing the run by
    hand; the gate assertions live in ``trace-harness replay``.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import Any

from trace_harness.attribution.schemas import AttributionResult
from trace_harness.environment.controls import reference_controls, resolve_guardrail
from trace_harness.environment.state import SupportState
from trace_harness.environment.support_env import SupportEnvironment
from trace_harness.environment.tools import ToolSideEffect
from trace_harness.models.base import ToolCall
from trace_harness.regression.schemas import (
    RegressionArtifact,
    ReplayMode,
    ReplayModeBasis,
    SiblingTest,
)
from trace_harness.tasks.schemas import TaskSpec
from trace_harness.tracing.events import TraceEvent, TraceEventType
from trace_harness.verifiers.base import VerifierResult


def _harness_version() -> str:
    """Installed package version, or a marker when running from a source tree."""
    try:
        return version("trace-harness")
    except PackageNotFoundError:  # pragma: no cover - depends on install mode
        return "unknown"


def _recorded_agent_actions(trace: list[TraceEvent]) -> list[dict[str, Any]]:
    """The agent's normalized moves, in trace order.

    MODEL_ACTION payloads are already ``AgentAction`` dumps minus the
    provider-specific ``raw`` blob, so they can be fed straight back into a
    fixture adapter on replay.
    """
    return [
        dict(event.payload)
        for event in trace
        if event.event_type is TraceEventType.MODEL_ACTION and event.payload
    ]


def classify_replay_mode(basis: ReplayModeBasis) -> ReplayMode:
    """All four conditions need affirmative evidence; missing facts fail closed."""
    if (
        basis.control_ids
        and basis.control_step is not None
        and basis.control_step == basis.first_irreversible_action_step
        and basis.rule_kind == "prohibition"
        and basis.gated_tool
        and basis.checks_reachable_via_gated_tool
        and set(basis.checks_reachable_via_gated_tool) <= set(basis.checks_covered_by_control)
        and not basis.other_irreversible_tools
    ):
        return "static_ok"
    return "live_required"


def _replay_mode_basis(
    task: TaskSpec,
    trace: list[TraceEvent],
    initial_state: dict[str, Any],
    attribution: AttributionResult,
    checks_reachable_by_tool: dict[str, list[str]],
) -> ReplayModeBasis:
    """Find the first reference-control block in an isolated copy of the pinned world.

    Only recorded, executed tool calls advance the sandbox. No model is run.
    Stop at the block: a recording cannot predict the agent's next decision.
    """
    env = SupportEnvironment(
        SupportState.model_validate(initial_state), available_tools=task.available_tools
    )
    controls = reference_controls()
    for control in controls:
        env.install_control(control)
    basis = ReplayModeBasis(
        control_ids=[control.control_id for control in controls],
        first_irreversible_action_step=attribution.first_irreversible_action_step,
    )
    for event in trace:
        if event.event_type is not TraceEventType.TOOL_CALL_EXECUTED:
            continue
        call = ToolCall.model_validate(event.payload)
        # Existing blocks/errors did not mutate the recorded world.
        if event.payload.get("status") != "ok":
            continue
        result = env.execute(call, step_id=event.step_id)
        if result.blocked_by:
            control = next(c for c in controls if c.control_id == result.blocked_by)
            registered = resolve_guardrail(control.guardrail_ref)
            basis.control_step = event.step_id
            basis.gated_tool = call.tool_name
            basis.checks_reachable_via_gated_tool = checks_reachable_by_tool.get(call.tool_name, [])
            basis.checks_covered_by_control = sorted(registered.checks_covered)
            basis.rule_kind = registered.rule_kind
            basis.steps_remaining_after_control = sum(
                e.event_type is TraceEventType.MODEL_ACTION
                and e.step_id is not None
                and event.step_id is not None
                and e.step_id > event.step_id
                for e in trace
            )
            break
    basis.other_irreversible_tools = sorted(
        tool
        for tool in task.available_tools
        if tool != basis.gated_tool
        and env.side_effect_for(tool) is ToolSideEffect.EXTERNAL_IRREVERSIBLE
    )
    return basis


def materialize_regression_artifact(
    *,
    task: TaskSpec,
    trace: list[TraceEvent],
    verifier_result: VerifierResult,
    initial_state: dict[str, Any],
    run_id: str,
    task_fixture_path: str | None,
    attribution: AttributionResult,
    checks_reachable_by_tool: dict[str, list[str]],
) -> RegressionArtifact:
    """Build a :class:`RegressionArtifact` from one verified failure."""
    if verifier_result.passed:
        raise ValueError("cannot materialize a regression from a passing run")

    fixture_path = task_fixture_path or f"runs/{run_id}/task_spec.json"
    siblings = [
        SiblingTest.model_validate(entry)
        for entry in task.metadata.get("positive_sibling_tasks", [])
    ]

    basis = _replay_mode_basis(task, trace, initial_state, attribution, checks_reachable_by_tool)
    return RegressionArtifact(
        replay_mode=classify_replay_mode(basis),
        replay_mode_basis=basis,
        test_name=f"regression_{task.task_id}",
        source_run_id=run_id,
        task_fixture=fixture_path,
        initial_state=initial_state,
        pinned_docs=list(initial_state.get("docs", [])),
        pinned_agent_actions=_recorded_agent_actions(trace),
        expected_behavior=task.expected_behavior,
        forbidden_actions=task.forbidden_actions,
        # Order-preserving dedupe: one check class can fail on N records.
        verifier_checks=list(
            dict.fromkeys(check.check_id for check in verifier_result.failed_checks)
        ),
        positive_sibling_tests=siblings,
        severity=verifier_result.severity or task.severity,
        blocks_release=verifier_result.blocks_release,
        replay_command=f"trace-harness run-pipeline {fixture_path}",
        metadata={
            "source_verifier_id": verifier_result.verifier_id,
            "available_tools": list(task.available_tools),
            "schema_versions": {
                "task": task.schema_version,
                "state": initial_state.get("schema_version"),
                "trace": trace[0].schema_version if trace else None,
                "verifier_result": verifier_result.schema_version,
                "harness": _harness_version(),
            },
        },
    )
