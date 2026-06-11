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
