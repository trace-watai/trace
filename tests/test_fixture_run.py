"""End-to-end fixture runs: artifacts, trace shape, side effects, fixture hygiene."""

from __future__ import annotations

import json
from collections import Counter

from conftest import (
    EXPECTED_VERIFIER_PATH,
    FIXTURES_DIR,
    run_task_fixture,
)
from trace_harness.models.fixture import FixtureScript
from trace_harness.runner.result import RunStatus, TerminationReason
from trace_harness.tasks.loader import load_docs_for_task, load_task
from trace_harness.tracing import artifact_store as names
from trace_harness.tracing.events import TraceEventType


def test_failure_run_completes_with_final_answer(failure_run):
    assert failure_run.result.status is RunStatus.COMPLETED
    assert failure_run.result.termination_reason is TerminationReason.FINAL_ANSWER
    assert failure_run.result.steps_taken == 7
    assert failure_run.result.final_output is not None
    assert "refund" in failure_run.result.final_output.lower()
    assert failure_run.result.error is None


def test_run_artifacts_written(failure_run):
    for name in (
        names.TASK_SPEC,
        names.RUN_CONFIG,
        names.INITIAL_STATE,
        names.TRACE,
        names.FINAL_STATE,
        names.RUN_RESULT,
    ):
        assert failure_run.store.exists(failure_run.run_id, name), f"missing {name}"
    # artifact_paths in the result must point at files that exist.
    for rel_path in failure_run.result.artifact_paths.values():
        assert (failure_run.store.runs_dir / rel_path).is_file()


def test_trace_contains_expected_event_shape(failure_run):
    counts = Counter(event.event_type for event in failure_run.trace)
    assert counts[TraceEventType.RUN_STARTED] == 1
    assert counts[TraceEventType.RUN_FINISHED] == 1
    assert counts[TraceEventType.TASK_LOADED] == 1
    assert counts[TraceEventType.STATE_SNAPSHOT] == 2  # initial + final
    assert counts[TraceEventType.MODEL_ACTION] == 7
    assert counts[TraceEventType.TOOL_CALL_REQUESTED] == 6
    assert counts[TraceEventType.TOOL_CALL_VALIDATED] == 6
    assert counts[TraceEventType.TOOL_CALL_EXECUTED] == 6
    assert counts[TraceEventType.TOOL_OBSERVATION] == 6
    assert counts[TraceEventType.RETRIEVAL_RESULT] == 3  # three search_docs calls
    assert counts[TraceEventType.FINAL_ANSWER] == 1
    # Step ids cover decisions 1..7 and event ids are unique.
    action_steps = [
        e.step_id for e in failure_run.trace if e.event_type is TraceEventType.MODEL_ACTION
    ]
    assert action_steps == list(range(1, 8))
    event_ids = [e.event_id for e in failure_run.trace]
    assert len(event_ids) == len(set(event_ids))


def test_retrieval_events_expose_doc_status(failure_run):
    retrievals = [e for e in failure_run.trace if e.event_type is TraceEventType.RETRIEVAL_RESULT]
    first = retrievals[0]
    statuses = {r["doc_id"]: r["status"] for r in first.payload["results"]}
    # The "refund policy" query surfaces both policies, with status metadata
    # visible — the agent had every chance to notice the deprecation.
    assert statuses["refund_policy_v2"] == "deprecated"
    assert statuses["refund_policy_v4"] == "current"


def test_final_state_records_side_effects(failure_run):
    refunds = failure_run.final_state["refunds"]
    tickets = failure_run.final_state["tickets"]
    assert len(refunds) == 1
    assert refunds[0]["refund_type"] == "cash"
    assert refunds[0]["amount_usd"] == 432.0
    assert refunds[0]["issued_at_step"] == 5
    assert len(tickets) == 1
    assert tickets[0]["created_at_step"] == 6
    assert "outage" in tickets[0]["notes"].lower()
    # The initial snapshot must be untouched by later mutations.
    assert failure_run.initial_state["refunds"] == []
    assert failure_run.initial_state["tickets"] == []


def test_valid_run_completes(valid_run):
    assert valid_run.result.status is RunStatus.COMPLETED
    assert valid_run.result.steps_taken == 5
    assert len(valid_run.final_state["refunds"]) == 1


def test_script_exhaustion_preserves_partial_artifacts(tmp_path):
    """A too-short script must terminate cleanly and keep every artifact."""
    task_path = FIXTURES_DIR / "tasks" / "refund_policy_failure.json"
    short_script = {
        "schema_version": "0.1.0",
        "script_id": "short",
        "task_id": "refund_policy_failure",
        "actions": [
            {
                "kind": "tool_call",
                "tool_call": {"tool_name": "search_docs", "arguments": {"query": "refund"}},
            }
        ],
    }
    script_path = tmp_path / "short_script.json"
    script_path.write_text(json.dumps(short_script))

    import trace_harness.tasks.loader as loader
    from trace_harness.environment.support_env import SupportEnvironment
    from trace_harness.models.fixture import FixtureModelAdapter
    from trace_harness.runner.agent_runner import AgentRunner
    from trace_harness.runner.config import RunConfig
    from trace_harness.tracing.artifact_store import ArtifactStore

    task = loader.load_task(task_path)
    docs = loader.load_docs_for_task(task, task_path)
    store = ArtifactStore(tmp_path / "runs")
    result = AgentRunner(
        FixtureModelAdapter.from_file(script_path),
        SupportEnvironment.from_task(task, docs=docs),
        store,
    ).run(task, RunConfig(task_id=task.task_id))

    assert result.status is RunStatus.TERMINATED
    assert result.termination_reason is TerminationReason.SCRIPT_EXHAUSTED
    assert result.steps_taken == 1
    trace = store.read_trace(result.run_id)
    assert any(e.event_type is TraceEventType.ERROR for e in trace)
    assert trace[-1].event_type is TraceEventType.RUN_FINISHED
    for name in (names.TASK_SPEC, names.INITIAL_STATE, names.FINAL_STATE, names.RUN_RESULT):
        assert store.exists(result.run_id, name)


def test_all_fixture_files_parse():
    """Every fixture in the repo must load through its schema."""
    task_paths = sorted((FIXTURES_DIR / "tasks").glob("*.json"))
    assert len(task_paths) >= 2
    for task_path in task_paths:
        task = load_task(task_path)
        docs = load_docs_for_task(task, task_path)
        # Doc-less tasks are legal; but a task that DECLARES a docs fixture
        # and gets nothing back has a broken reference.
        if task.docs_fixture is not None:
            assert docs, f"{task_path.name} declares a docs_fixture but loaded no docs"
        script_rel = task.metadata.get("fixture_script")
        assert script_rel, f"{task_path.name} has no metadata.fixture_script"
        script = FixtureScript.model_validate(
            json.loads((task_path.parent / script_rel).resolve().read_text())
        )
        assert script.task_id == task.task_id
        assert script.actions[-1].kind.value == "final_answer", (
            "scripts should end in a final answer so runs terminate cleanly"
        )
    assert json.loads(EXPECTED_VERIFIER_PATH.read_text())["expected"]["passed"] is False


def test_failure_and_valid_runs_are_deterministic(tmp_path):
    """Two runs of the same fixture differ only in run ids and timestamps."""
    from conftest import FAILURE_TASK_PATH

    first = run_task_fixture(FAILURE_TASK_PATH, tmp_path / "a")
    second = run_task_fixture(FAILURE_TASK_PATH, tmp_path / "b")
    assert first.final_state == second.final_state
    stripped = [
        [
            {k: v for k, v in e.model_dump(mode="json").items() if k not in ("run_id", "timestamp")}
            for e in run.trace
        ]
        for run in (first, second)
    ]
    assert stripped[0] == stripped[1]


# --- review-hardening regressions -------------------------------------------


def test_agent_runner_refuses_reuse(tmp_path):
    """State and script cursors are stateful; a second run() must fail loudly."""
    import pytest

    from conftest import FAILURE_TASK_PATH
    from trace_harness.environment.support_env import SupportEnvironment
    from trace_harness.models.fixture import FixtureModelAdapter
    from trace_harness.runner.agent_runner import AgentRunner
    from trace_harness.runner.config import RunConfig
    from trace_harness.tracing.artifact_store import ArtifactStore

    task = load_task(FAILURE_TASK_PATH)
    docs = load_docs_for_task(task, FAILURE_TASK_PATH)
    script = (FAILURE_TASK_PATH.parent / task.metadata["fixture_script"]).resolve()
    runner = AgentRunner(
        FixtureModelAdapter.from_file(script),
        SupportEnvironment.from_task(task, docs=docs),
        ArtifactStore(tmp_path / "runs"),
    )
    runner.run(task, RunConfig(task_id=task.task_id))
    with pytest.raises(RuntimeError, match="single-run"):
        runner.run(task, RunConfig(task_id=task.task_id))


def test_typoed_initial_state_keys_fail_loudly():
    """extra='forbid' across state models: 'ordes' must raise, not vanish."""
    import pytest
    from pydantic import ValidationError

    from trace_harness.environment.state import Order, SupportState

    with pytest.raises(ValidationError):
        SupportState.model_validate({"ordes": []})
    with pytest.raises(ValidationError):
        Order(
            order_id="O1",
            customer_name="J",
            plan="p",
            amount_usd=10.0,
            purchase_age_days=-47,  # sign typo must not silently authorize refunds
        )


def test_seeded_side_effect_ids_do_not_collide():
    """Pre-existing REF-0001 in initial_state must bump the generated sequence."""
    from trace_harness.environment.state import SupportState
    from trace_harness.tasks.schemas import TaskSpec

    task = TaskSpec(
        task_id="t",
        title="t",
        description="t",
        goal="t",
        workflow_type="w",
        initial_state={
            "orders": [],
            "refunds": [
                {
                    "refund_id": "REF-0001",
                    "order_id": "O0",
                    "customer_name": "J",
                    "refund_type": "cash",
                    "amount_usd": 1.0,
                    "reason": "history",
                }
            ],
            "tickets": [],
        },
        available_tools=[],
        available_docs=[],
    )
    state = SupportState.from_task(task)
    assert state.next_refund_seq == 2


def test_invalid_tool_call_omits_executed_event(tmp_path):
    """Runner skips TOOL_CALL_EXECUTED for invalid calls; valid call on next step still executes."""
    task_path = FIXTURES_DIR / "tasks" / "refund_policy_failure.json"
    script = {
        "schema_version": "0.1.0",
        "script_id": "invalid_recovery",
        "task_id": "refund_policy_failure",
        "actions": [
            {
                "kind": "tool_call",
                "tool_call": {"tool_name": "does_not_exist", "arguments": {}},
            },
            {
                "kind": "tool_call",
                "tool_call": {"tool_name": "search_docs", "arguments": {"query": "refund"}},
            },
            {
                "kind": "final_answer",
                "final_answer": "Checked the policy. No refund applicable.",
            },
        ],
    }
    script_path = tmp_path / "invalid_recovery_script.json"
    script_path.write_text(json.dumps(script))

    import trace_harness.tasks.loader as loader
    from trace_harness.environment.support_env import SupportEnvironment
    from trace_harness.models.fixture import FixtureModelAdapter
    from trace_harness.runner.agent_runner import AgentRunner
    from trace_harness.runner.config import RunConfig
    from trace_harness.tracing.artifact_store import ArtifactStore

    task = loader.load_task(task_path)
    docs = loader.load_docs_for_task(task, task_path)
    store = ArtifactStore(tmp_path / "runs")
    result = AgentRunner(
        FixtureModelAdapter.from_file(script_path),
        SupportEnvironment.from_task(task, docs=docs),
        store,
    ).run(task, RunConfig(task_id=task.task_id))

    trace = store.read_trace(result.run_id)
    counts = Counter(event.event_type for event in trace)

    # Invalid call: REQUESTED + VALIDATED(valid=False) + OBSERVATION, but NO EXECUTED.
    # Valid call: REQUESTED + VALIDATED(valid=True) + EXECUTED + OBSERVATION.
    assert counts[TraceEventType.TOOL_CALL_REQUESTED] == 2
    assert counts[TraceEventType.TOOL_CALL_VALIDATED] == 2
    assert counts[TraceEventType.TOOL_CALL_EXECUTED] == 1
    assert counts[TraceEventType.TOOL_OBSERVATION] == 2

    validated = [e for e in trace if e.event_type is TraceEventType.TOOL_CALL_VALIDATED]
    assert validated[0].payload["valid"] is False
    assert "does_not_exist" in validated[0].payload["error"]
    assert validated[1].payload["valid"] is True


def test_retrieval_result_events_all_fields_present(failure_run):
    """Every RETRIEVAL_RESULT result has all provenance fields; content excluded."""
    retrieval_events = [
        e for e in failure_run.trace if e.event_type is TraceEventType.RETRIEVAL_RESULT
    ]
    assert len(retrieval_events) > 0
    for event in retrieval_events:
        payload = event.payload
        assert payload["result_count"] == len(payload["results"])
        for result in payload["results"]:
            for field in ("doc_id", "status", "title", "score", "source"):
                assert field in result, (
                    f"retrieval result missing '{field}' at step {event.step_id}"
                )
            # Content lives in tool_observation, not in the retrieval event.
            assert "content" not in result


def test_state_snapshot_structure(failure_run):
    """Both initial and final state snapshots have all required top-level fields."""
    for state in (failure_run.initial_state, failure_run.final_state):
        for key in (
            "schema_version",
            "orders",
            "refunds",
            "tickets",
            "docs",
            "next_refund_seq",
            "next_ticket_seq",
        ):
            assert key in state, f"state snapshot missing '{key}'"
        assert isinstance(state["orders"], list)
        assert isinstance(state["refunds"], list)
        assert isinstance(state["tickets"], list)
        assert isinstance(state["docs"], list)
    # Provenance must be set so the verifier can link side effects to trace steps.
    for refund in failure_run.final_state["refunds"]:
        assert refund["issued_at_step"] is not None
    for ticket in failure_run.final_state["tickets"]:
        assert ticket["created_at_step"] is not None


def test_run_result_written_even_if_final_snapshot_fails(tmp_path):
    """The partial-artifacts promise: a dying snapshot must not lose run_result."""
    from conftest import FAILURE_TASK_PATH
    from trace_harness.environment.support_env import SupportEnvironment
    from trace_harness.models.fixture import FixtureModelAdapter
    from trace_harness.runner.agent_runner import AgentRunner
    from trace_harness.runner.config import RunConfig
    from trace_harness.tracing.artifact_store import ArtifactStore

    class FlakySnapshotEnv:
        """Delegates everything; snapshot_state works once, then dies."""

        def __init__(self, inner):
            self._inner = inner
            self._snapshots = 0

        def tool_specs(self):
            return self._inner.tool_specs()

        def validate_call(self, call):
            return self._inner.validate_call(call)

        def execute(self, call, step_id=None):
            return self._inner.execute(call, step_id=step_id)

        def side_effect_for(self, tool_name):
            return self._inner.side_effect_for(tool_name)

        def snapshot_state(self):
            self._snapshots += 1
            if self._snapshots > 1:
                raise RuntimeError("environment exploded during final snapshot")
            return self._inner.snapshot_state()

    task = load_task(FAILURE_TASK_PATH)
    docs = load_docs_for_task(task, FAILURE_TASK_PATH)
    script = (FAILURE_TASK_PATH.parent / task.metadata["fixture_script"]).resolve()
    store = ArtifactStore(tmp_path / "runs")
    runner = AgentRunner(
        FixtureModelAdapter.from_file(script),
        FlakySnapshotEnv(SupportEnvironment.from_task(task, docs=docs)),
        store,
    )
    result = runner.run(task, RunConfig(task_id=task.task_id))  # must not raise
    assert result.status is RunStatus.ERROR
    assert "snapshot failed" in (result.error or "")
    assert store.exists(result.run_id, names.RUN_RESULT)
    assert store.read_json(result.run_id, names.FINAL_STATE)["snapshot_error"]
