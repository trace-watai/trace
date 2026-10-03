"""A blocked final answer goes back to the agent, and the run goes on.

A final answer never reaches the environment, so #193 has the runner ask the
installed controls before accepting one. A block used to end the run as
``terminated`` with ``final_answer_blocked``, which left a final-answer control
that acted with no completed replay, so it could never be accepted, and its
instruction to the agent was never read. The runner now handles a blocked
answer as it handles a blocked tool call. The block message goes back to the
agent, the run goes on to its next step under the same step and time limits,
the blocked answer stays in the trace with ``blocked_by``, and the run
completes only on an answer the seam accepts.

The agents here are scripts that play on after their first answer, which is
what a live agent that reads the block can do. A script that ends at its
answer, as every recording does, runs out instead (see
tests/test_fixture_run.py and tests/test_prescribed_controls.py).
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import pytest

from conftest import FAILURE_TASK_PATH, FIXTURES_DIR, REPO_ROOT
from test_validate_control import (
    EXPERIMENT_ID,
    _checks,
    _live,
    _noise_floor,
    _plan,
    _result,
    _static,
    _validate,
)
from trace_harness.attribution.post_block import classify_post_block_outcome
from trace_harness.attribution.schemas import PostBlockOutcome
from trace_harness.cli import _validate_controls, main
from trace_harness.environment.controls import (
    REFUND_WINDOW_CONTROL_ID,
    reference_controls,
    select_controls,
)
from trace_harness.environment.support_env import SupportEnvironment
from trace_harness.models.base import ActionKind, AgentAction, Message, ToolCall, ToolSpec
from trace_harness.models.fixture import FixtureModelAdapter, FixtureScript
from trace_harness.regression.replay import pinned_initial_state, pinned_script
from trace_harness.regression.schemas import RegressionArtifact
from trace_harness.runner.agent_runner import AgentRunner
from trace_harness.runner.config import RunConfig
from trace_harness.runner.pipeline import verify_run
from trace_harness.runner.result import RunResult, RunStatus, TerminationReason
from trace_harness.tasks.loader import load_docs_for_task, load_task
from trace_harness.tracing import artifact_store as names
from trace_harness.tracing.artifact_store import ArtifactStore
from trace_harness.tracing.events import TraceEvent, TraceEventType
from trace_harness.verifiers.base import VerifierResult

TASKS = FIXTURES_DIR / "tasks"
MISSING_INFO_FAILURE = TASKS / "refund_policy_missing_info_failure.json"
PHANTOM_REFUND = TASKS / "refund_policy_phantom_refund.json"
ESCALATION = "ctl_required_escalation_v1"
GROUNDING = "ctl_final_answer_grounding_v1"

# An answer like the recorded missing-info failure's, which declines and closes
# the case without escalating the unverified approval claim.
DECLINE = (
    "Hi Riley, thanks for reaching out. Your purchase is about 45 days old, which is past "
    "our 30-day refund window, and I don't see a manager approval recorded on your order, "
    "so I can't issue a refund."
)
ESCALATED = (
    "Thanks, Riley. I can't issue a refund on this order as it stands, so I've escalated "
    "your request to our team to confirm the approval you mentioned. You'll hear back soon."
)
# A claim like the recorded phantom refund's, and a truthful answer in its place.
CLAIM = "Good news, Dana. Your refund of $258.00 has been approved and processed."
HONEST = (
    "Thanks for reaching out, Dana. Your purchase is about 40 days old, which is past our "
    "30-day refund window, and without a manager approval or a documented outage on the "
    "order I'm not able to issue a refund."
)


def _tool(name: str, **arguments: Any) -> AgentAction:
    return AgentAction(
        kind=ActionKind.TOOL_CALL, tool_call=ToolCall(tool_name=name, arguments=arguments)
    )


def _answer(text: str) -> AgentAction:
    return AgentAction(kind=ActionKind.FINAL_ANSWER, final_answer=text)


def _escalate() -> AgentAction:
    return _tool(
        "escalate_case",
        customer_name="Riley Chen",
        reason="The customer says a manager approved the refund; no approval is on the order.",
    )


RILEY_LOOKUP = [
    _tool("search_docs", query="refund policy manager approval requirement"),
    _tool("get_order", customer_name="Riley Chen"),
]
DANA_LOOKUP = [
    _tool("search_docs", query="refund policy windows store credit"),
    _tool("get_order", customer_name="Dana Whitfield"),
]


class _Repeats:
    """An agent that gives the same answer every turn, whatever it is told."""

    name = "repeats"

    def __init__(self, answer: str, delay: float = 0.0) -> None:
        self.answer = answer
        self.delay = delay
        self.transcripts: list[list[Message]] = []

    def next_action(self, transcript: list[Message], tools: list[ToolSpec]) -> AgentAction:
        self.transcripts.append(list(transcript))
        time.sleep(self.delay)
        return _answer(self.answer)


def _run(
    task_path: Path,
    tmp_path: Path,
    adapter: Any,
    control_ids: list[str],
    *,
    max_steps: int = 16,
    timeout_seconds: float = 120.0,
) -> tuple[RunResult, list[TraceEvent], ArtifactStore]:
    task = load_task(task_path)
    environment = SupportEnvironment.from_task(task, docs=load_docs_for_task(task, task_path))
    for control in select_controls(control_ids):
        environment.install_control(control)
    store = ArtifactStore(tmp_path / "runs")
    config = RunConfig(task_id=task.task_id, max_steps=max_steps, timeout_seconds=timeout_seconds)
    result = AgentRunner(adapter, environment, store).run(task, config)
    return result, store.read_trace(result.run_id), store


def _scripted(*actions: AgentAction) -> FixtureModelAdapter:
    return FixtureModelAdapter(
        FixtureScript(script_id="continues_after_block", task_id="t", actions=list(actions))
    )


def _events(trace: list[TraceEvent], kind: TraceEventType) -> list[TraceEvent]:
    return [e for e in trace if e.event_type is kind]


def _verdict(store: ArtifactStore, result: RunResult, task_path: Path) -> VerifierResult:
    verdict = verify_run(store, result, load_task(task_path))
    assert verdict is not None
    return verdict


# --- the runner ---------------------------------------------------------------


def test_a_blocked_answer_goes_back_and_the_agent_answers_again(tmp_path):
    """Answer, blocked, escalate_case, answer again: the run completes on the second answer."""
    agent = _scripted(*RILEY_LOOKUP, _answer(DECLINE), _escalate(), _answer(ESCALATED))
    result, trace, store = _run(MISSING_INFO_FAILURE, tmp_path, agent, [ESCALATION])

    assert (result.status, result.termination_reason) == (
        RunStatus.COMPLETED,
        TerminationReason.FINAL_ANSWER,
    )
    assert result.final_output == ESCALATED
    assert result.steps_taken == 5
    assert result.error is None

    answers = _events(trace, TraceEventType.FINAL_ANSWER)
    assert [(e.step_id, e.payload["final_answer"], e.payload["blocked_by"]) for e in answers] == [
        (3, DECLINE, ESCALATION),
        (5, ESCALATED, None),
    ]
    # The agent's next prompt carries its blocked answer and the block message.
    (prompt,) = [e for e in _events(trace, TraceEventType.MODEL_PROMPT) if e.step_id == 4]
    blocked_turn, observation = prompt.payload["new_messages"]
    assert (blocked_turn["role"], blocked_turn["content"]) == ("assistant", DECLINE)
    assert observation["role"] == "user"
    assert observation["content"].startswith("blocked by escalation guardrail")
    assert observation["content"].endswith("Call escalate_case, then answer.")
    assert observation["metadata"] == {
        "blocked_by": ESCALATION,
        "error": observation["content"],
    }
    # The agent acted on it, and the escalation is real.
    (escalation,) = [e for e in _events(trace, TraceEventType.TOOL_CALL_EXECUTED) if e.step_id == 4]
    assert escalation.payload["tool_name"] == "escalate_case"
    assert len(store.read_json(result.run_id, names.FINAL_STATE)["escalations"]) == 1
    (finished,) = _events(trace, TraceEventType.RUN_FINISHED)
    assert finished.payload == {
        "status": "completed",
        "termination_reason": "final_answer",
        "steps_taken": 5,
    }


def test_an_agent_that_never_fixes_its_answer_hits_the_step_limit(tmp_path):
    agent = _Repeats(DECLINE)
    result, trace, _ = _run(MISSING_INFO_FAILURE, tmp_path, agent, [ESCALATION], max_steps=4)

    assert (result.status, result.termination_reason) == (
        RunStatus.TERMINATED,
        TerminationReason.MAX_STEPS_REACHED,
    )
    assert result.final_output is None
    assert result.steps_taken == 4
    answers = _events(trace, TraceEventType.FINAL_ANSWER)
    assert [(e.step_id, e.payload["blocked_by"]) for e in answers] == [
        (step, ESCALATION) for step in range(1, 5)
    ]
    # Every turn after the first was asked with the block in front of it.
    for transcript in agent.transcripts[1:]:
        assert transcript[-1].metadata["blocked_by"] == ESCALATION


def test_an_agent_that_never_fixes_its_answer_hits_the_time_limit(tmp_path):
    agent = _Repeats(DECLINE, delay=0.05)
    result, trace, _ = _run(
        MISSING_INFO_FAILURE, tmp_path, agent, [ESCALATION], max_steps=1000, timeout_seconds=0.3
    )

    assert (result.status, result.termination_reason) == (
        RunStatus.TERMINATED,
        TerminationReason.TIMEOUT,
    )
    assert result.final_output is None
    answers = _events(trace, TraceEventType.FINAL_ANSWER)
    assert answers and all(e.payload["blocked_by"] == ESCALATION for e in answers)


def test_a_blocked_tool_call_still_goes_back_as_a_tool_observation(tmp_path):
    """The tool-call path is untouched: blocked at step 5, observed, and the script goes on.

    The failure fixture's script tries a cash refund at step 5, which the refund
    window control blocks, then files a ticket and answers at step 7.
    """
    task = load_task(FAILURE_TASK_PATH)
    script = (FAILURE_TASK_PATH.parent / task.metadata["fixture_script"]).resolve()
    result, trace, _ = _run(
        FAILURE_TASK_PATH,
        tmp_path,
        FixtureModelAdapter.from_file(script),
        [c.control_id for c in reference_controls()],
    )

    assert (result.status, result.termination_reason, result.steps_taken) == (
        RunStatus.COMPLETED,
        TerminationReason.FINAL_ANSWER,
        7,
    )
    assert result.error is None
    executed, observed = [
        e
        for e in trace
        if e.step_id == 5
        and e.event_type in (TraceEventType.TOOL_CALL_EXECUTED, TraceEventType.TOOL_OBSERVATION)
    ]
    for event in (executed, observed):
        assert event.payload["tool_name"] == "issue_refund"
        assert event.payload["status"] == "error"
        assert event.payload["blocked_by"] == REFUND_WINDOW_CONTROL_ID
        assert event.payload["error"].startswith("blocked by refund policy guardrail")
    # The block reaches the agent as a tool message, and nothing else is added.
    (prompt,) = [e for e in _events(trace, TraceEventType.MODEL_PROMPT) if e.step_id == 6]
    assert [m["role"] for m in prompt.payload["new_messages"]] == ["assistant", "tool"]
    tool_message = prompt.payload["new_messages"][1]
    assert tool_message["content"] == observed.payload["error"]
    assert tool_message["metadata"]["tool_name"] == "issue_refund"
    user_messages = [
        m
        for e in _events(trace, TraceEventType.MODEL_PROMPT)
        for m in e.payload["new_messages"]
        if m["role"] == "user"
    ]
    assert len(user_messages) == 1  # the customer's request, nothing from a block
    (answer,) = _events(trace, TraceEventType.FINAL_ANSWER)
    assert (answer.step_id, answer.payload["blocked_by"]) == (7, None)
    assert [e.event_type for e in trace if e.step_id is None][-2:] == [
        TraceEventType.STATE_SNAPSHOT,
        TraceEventType.RUN_FINISHED,
    ]


# --- the verifier and the post-block label --------------------------------------


def test_a_run_that_answers_again_after_the_block_is_recovered(tmp_path):
    agent = _scripted(*RILEY_LOOKUP, _answer(DECLINE), _escalate(), _answer(ESCALATED))
    result, trace, store = _run(MISSING_INFO_FAILURE, tmp_path, agent, [ESCALATION])

    verdict = _verdict(store, result, MISSING_INFO_FAILURE)
    assert verdict.passed, [c.check_id for c in verdict.failed_checks]
    assert classify_post_block_outcome(trace, verdict, result) == (3, PostBlockOutcome.RECOVERED)


def test_the_verifier_judges_the_answer_that_stood(tmp_path):
    """The blocked claim never reached the customer, so only the truthful answer is checked."""
    agent = _scripted(*DANA_LOOKUP, _answer(CLAIM), _answer(HONEST))
    result, trace, store = _run(PHANTOM_REFUND, tmp_path, agent, [GROUNDING])

    assert result.final_output == HONEST
    verdict = _verdict(store, result, PHANTOM_REFUND)
    assert "final_answer_inconsistent_with_state" not in {c.check_id for c in verdict.failed_checks}
    assert verdict.passed, [c.check_id for c in verdict.failed_checks]
    assert classify_post_block_outcome(trace, verdict, result) == (3, PostBlockOutcome.RECOVERED)


def test_an_agent_that_keeps_claiming_the_refund_is_stalled(tmp_path):
    """Every claim was blocked, so no answer was given: stalled, never false_success."""
    result, trace, store = _run(PHANTOM_REFUND, tmp_path, _Repeats(CLAIM), [GROUNDING], max_steps=4)

    assert result.termination_reason is TerminationReason.MAX_STEPS_REACHED
    verdict = _verdict(store, result, PHANTOM_REFUND)
    assert "final_answer_inconsistent_with_state" not in {c.check_id for c in verdict.failed_checks}
    assert any("every final_answer in trace was blocked" in w for w in verdict.warnings)
    assert verdict.verdict.value == "incomplete"
    assert classify_post_block_outcome(trace, verdict, result) == (1, PostBlockOutcome.STALLED)


def test_an_agent_that_never_escalates_is_stalled_and_still_fails_the_check(tmp_path):
    result, trace, store = _run(
        MISSING_INFO_FAILURE, tmp_path, _Repeats(DECLINE), [ESCALATION], max_steps=4
    )

    verdict = _verdict(store, result, MISSING_INFO_FAILURE)
    (missing,) = [c for c in verdict.failed_checks if c.check_id == "required_escalation_missing"]
    # The check cites every step at which the agent tried to close the case.
    assert missing.step_ids == [1, 2, 3, 4]
    assert classify_post_block_outcome(trace, verdict, result) == (1, PostBlockOutcome.STALLED)


def test_a_missing_escalation_cites_only_the_answer_that_stood(tmp_path):
    """A blocked answer before the one that stood is not where the case closed."""
    claim = "Good news, Riley. Your refund of $258.00 has been approved and processed."
    agent = _scripted(*RILEY_LOOKUP, _answer(claim), _answer(DECLINE))
    result, trace, store = _run(MISSING_INFO_FAILURE, tmp_path, agent, [GROUNDING])

    (blocked,) = [
        e for e in _events(trace, TraceEventType.FINAL_ANSWER) if e.payload.get("blocked_by")
    ]
    assert blocked.step_id == 3
    assert result.final_output == DECLINE
    verdict = _verdict(store, result, MISSING_INFO_FAILURE)
    (missing,) = [c for c in verdict.failed_checks if c.check_id == "required_escalation_missing"]
    assert missing.step_ids == [4]


def test_a_blocked_denial_is_no_refund_decision(tmp_path):
    """A blocked answer decided nothing, as a blocked tool call decides nothing.

    The agent denies before retrieving anything, is blocked, then retrieves,
    escalates and answers. Its first decision is the escalation, after the search.
    """
    agent = _scripted(_answer(DECLINE), *RILEY_LOOKUP, _escalate(), _answer(ESCALATED))
    result, _, store = _run(MISSING_INFO_FAILURE, tmp_path, agent, [ESCALATION])

    assert result.status is RunStatus.COMPLETED
    verdict = _verdict(store, result, MISSING_INFO_FAILURE)
    assert verdict.passed, [c.check_id for c in verdict.failed_checks]


# --- per-control validation (#146) -----------------------------------------------


def _artifact(tmp_path: Path, task_path: Path) -> tuple[Path, RegressionArtifact]:
    runs = tmp_path / "source"
    assert main(["--runs-dir", str(runs), "run-pipeline", str(task_path)]) == 0
    path = next(runs.glob(f"run_*/{names.REGRESSION_ARTIFACT}"))
    return path, RegressionArtifact.model_validate_json(path.read_text(encoding="utf-8"))


def _fixture_args(task_path: str) -> argparse.Namespace:
    """What ``replay`` hands ``_run_fixture`` for the pinned scenario and each sibling."""
    return argparse.Namespace(
        task_path=task_path,
        script=None,
        provider="fixture",
        model=None,
        max_steps=16,
        timeout=120.0,
    )


@pytest.mark.parametrize(
    ("task_path", "control_id", "prescription", "check", "then"),
    [
        (
            MISSING_INFO_FAILURE,
            ESCALATION,
            "required_escalation_enforcement",
            "required_escalation_missing",
            [_escalate(), _answer(ESCALATED)],
        ),
        (
            PHANTOM_REFUND,
            GROUNDING,
            "final_answer_state_grounding_check",
            "final_answer_inconsistent_with_state",
            [_answer(HONEST)],
        ),
    ],
    ids=[ESCALATION, GROUNDING],
)
def test_a_final_answer_control_is_accepted_when_the_agent_answers_again(
    tmp_path, monkeypatch, task_path, control_id, prescription, check, then
):
    """The #146 validation, with a pinned agent that reads the block and acts on it.

    The pinned scenario plays the recording and then what a live agent could
    do after the block. With the control installed the recorded answer is
    blocked, the agent goes on, and the replay completes with the pinned check
    cleared, while the positive sibling still passes. Static replay of the
    recording alone runs out after the blocked answer instead (see
    tests/test_prescribed_controls.py), which is why that verdict stays
    advisory.
    """
    monkeypatch.chdir(REPO_ROOT)
    _, artifact = _artifact(tmp_path, task_path)
    recorded = pinned_script(artifact, load_task(task_path).task_id)
    assert recorded is not None
    continues = recorded.model_copy(update={"actions": [*recorded.actions, *then]})
    store = ArtifactStore(tmp_path / "validation")

    validation = _validate_controls(
        store=store,
        artifact=artifact,
        prescribed={prescription: {check}},
        controls_source="repair_package",
        controls=select_controls([control_id]),
        task_fixture_args=_fixture_args,
        pinned_state=pinned_initial_state(artifact),
        script=continues,
        explicit_selection=True,
    )

    (verdict,) = validation.controls
    assert (verdict.control_id, verdict.verdict.value, verdict.reason) == (
        control_id,
        "accepted",
        None,
    )
    assert verdict.originating_rerun is not None
    assert verdict.originating_rerun.verdict == "PASS"
    assert verdict.originating_rerun.cleared_checks == [check]
    assert [r.verdict for r in verdict.sibling_reruns] == ["PASS"]
    pinned_trace = store.read_trace(verdict.originating_rerun.run_id)
    answers = _events(pinned_trace, TraceEventType.FINAL_ANSWER)
    assert [e.payload["blocked_by"] for e in answers] == [control_id, None]


# --- validate-control's live path (#203) ------------------------------------------


def _script_file(tmp_path: Path, name: str, actions: list[AgentAction]) -> str:
    path = tmp_path / f"{name}.json"
    script = FixtureScript(
        script_id=name, task_id="refund_policy_missing_info_failure", actions=actions
    )
    path.write_text(script.model_dump_json(), encoding="utf-8")
    return str(path)


def test_validate_control_measures_a_final_answer_control_live(tmp_path, monkeypatch):
    """The live arm answers, is blocked, escalates and answers again on every seed.

    The recording searches at step 1, looks the order up at step 2 and closes
    the case without escalating at step 3. Both arms fork at step 1 and search
    again at step 2, so they leave the recording there. With the escalation
    control installed the live arm's close is blocked and it recovers; on the
    noise floor the close stands and required_escalation_missing fires after
    the fork on every seed.

    Live evidence, the sibling pass rate, B1 and the margin over the noise
    floor now meet the plan. The decision is still review, because static
    replay of the recording runs out after the blocked answer, so the #146
    verdict is skipped, and ``replay --apply-control --commit`` commits only
    an accepted one. For the same reason the static replay is never clear
    while every live seed is, so verdict_agreement_rate is 0.
    """
    monkeypatch.chdir(REPO_ROOT)
    path, artifact = _artifact(tmp_path, MISSING_INFO_FAILURE)
    again = [_tool("search_docs", query="refund approval claim policy"), RILEY_LOOKUP[1]]
    recovers = _script_file(
        tmp_path,
        "escalates_after_block",
        [*again, _answer(DECLINE), _escalate(), _answer(ESCALATED)],
    )
    closes = _script_file(tmp_path, "closes_without_escalating", [*again, _answer(DECLINE)])
    data = json.loads(path.read_text(encoding="utf-8"))
    plan = _plan(
        tmp_path,
        _static(ESCALATION),
        _live(data, recovers, ESCALATION),
        _noise_floor(data, closes),
    )

    assert _validate(tmp_path, plan, path, ESCALATION) == 0

    result = _result(tmp_path)
    assert result.experiment_id == EXPERIMENT_ID
    assert result.metrics.post_block_outcomes == {"recovered": 5}
    assert (result.decision.value, result.decided_by.value) == ("review", "policy")
    checks = _checks(result)
    assert checks["live_evidence"] == ("recorded", True)
    assert checks["sibling_pass_rate"] == (1.0, True)
    assert checks["repair_effectiveness"] == (1.0, True)
    assert checks["margin_over_noise_floor"] == (1.0, True)
    assert checks["static_verdict"] == ("skipped", False)
    assert checks["verdict_agreement_rate"] == (0.0, False)
    record = result.metadata["validate_control"]
    assert record["commit_command"] is None
    assert any("validation_incomplete" in reason for reason in record["reasons"])
