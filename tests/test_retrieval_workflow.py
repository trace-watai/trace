"""TRA-136: real fixtures through the common runner, bundle, and replay paths."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from conftest import FIXTURES_DIR
from trace_harness.cli import main
from trace_harness.environment.controls import select_controls
from trace_harness.environment.factory import create_environment
from trace_harness.environment.retrieval_state import (
    Citation,
    RetrievalAnswer,
    RetrievalState,
    sections_for,
)
from trace_harness.models.base import ToolCall
from trace_harness.regression.schemas import RegressionArtifact
from trace_harness.runner.batch import BatchRunner
from trace_harness.runner.pipeline import run_task_pipeline, verify_run
from trace_harness.runner.suite import AgentConfig, load_suite
from trace_harness.tasks.loader import load_docs_for_task, load_task
from trace_harness.tracing import artifact_store as names
from trace_harness.tracing.artifact_store import ArtifactStore
from trace_harness.tracing.events import TraceEventType
from trace_harness.verifiers.base import VerifierInput, VerifierVerdict
from trace_harness.verifiers.retrieval_answer import RetrievalAnswerVerifier

TASKS = FIXTURES_DIR / "tasks" / "retrieval"
SUITE = FIXTURES_DIR / "suites" / "retrieval_v0.json"
PINS = json.loads((FIXTURES_DIR / "expected" / "retrieval_v0.json").read_text())
RECOVERY = FIXTURES_DIR / "scripts" / "retrieval" / "stale_section_recovery.json"
CONTROL_ID = "ctl_retrieval_current_section_v1"


@pytest.fixture(scope="module")
def suite_runs(tmp_path_factory):
    store = ArtifactStore(tmp_path_factory.mktemp("retrieval-suite"))
    summary = BatchRunner(store).run(load_suite(SUITE))
    assert len(summary.entries) == 16
    runs = {entry.task_id: entry.run_id for entry in summary.entries}
    assert set(runs) == set(PINS)
    return store, runs


@pytest.mark.parametrize("task_id", list(PINS))
def test_exact_verdicts_and_attribution_are_pinned(suite_runs, task_id):
    store, runs = suite_runs
    run_id = runs[task_id]
    result = store.read_json(run_id, names.RUN_RESULT)
    verdict = store.read_json(run_id, names.VERIFIER_RESULT)
    pin = PINS[task_id]
    assert result["status"] == "completed"
    assert sorted({c["check_id"] for c in verdict["failed_checks"]}) == pin["failed_checks"]
    assert verdict["passed"] is (not pin["failed_checks"])
    trace = store.read_trace(run_id)
    for event in trace:
        assert event.typed_payload is not None
    if not pin["failed_checks"]:
        assert not store.artifact_path(run_id, names.FAILURE_CARD).exists()
        return
    attribution = store.read_json(run_id, names.ATTRIBUTION_RESULT)
    for field, value in pin["attribution"].items():
        assert attribution[field] == value, field
    for check in pin["failed_checks"]:
        assert any(check in note for note in attribution["ambiguity_notes"])
    artifact = RegressionArtifact.model_validate(store.read_json(run_id, names.REGRESSION_ARTIFACT))
    assert artifact.pinned_docs == store.read_json(run_id, names.INITIAL_STATE)["docs"]
    assert artifact.pinned_agent_actions
    assert len(artifact.positive_sibling_tests) == 1
    assert store.read_json(run_id, names.REPAIR_PACKAGE)["controls"]


@pytest.mark.parametrize("task_id", [t for t in PINS if PINS[t]["failed_checks"]])
def test_every_negative_and_sibling_replay(suite_runs, task_id, tmp_path, capsys):
    store, runs = suite_runs
    artifact_path = store.artifact_path(runs[task_id], names.REGRESSION_ARTIFACT)
    assert main(["--runs-dir", str(tmp_path), "replay", str(artifact_path)]) == 0
    assert "regression gate clear" in capsys.readouterr().out


def environment(task_id="retrieval_stale_document"):
    path = TASKS / f"{task_id}.json"
    task = load_task(path)
    return create_environment(task, docs=load_docs_for_task(task, path))


def verifier_input(suite_runs, task_id="retrieval_bad_span_positive"):
    store, runs = suite_runs
    run_id = runs[task_id]
    return VerifierInput(
        task=load_task(TASKS / f"{task_id}.json"),
        trace=store.read_trace(run_id),
        initial_state=store.read_json(run_id, names.INITIAL_STATE),
        final_state=store.read_json(run_id, names.FINAL_STATE),
        run_id=run_id,
    )


def rewrite_answer(input, transform):
    input = input.model_copy(deep=True)
    event = next(e for e in input.trace if e.event_type is TraceEventType.FINAL_ANSWER)
    answer = json.loads(event.payload["final_answer"])
    transform(answer)
    event.payload["final_answer"] = json.dumps(answer)
    return input


def test_section_spans_are_real_content_and_reads_are_replayable():
    env = environment()
    search = env.execute(ToolCall(tool_name="search_docs", arguments={"query": "retention"}), 1)
    assert search.status == "ok"
    assert search.retrieval
    for hit in search.retrieval:
        doc = next(d for d in env.state.docs if d.doc_id == hit.doc_id)
        assert doc.content[hit.span_start : hit.span_end] == hit.content
        read = env.execute(
            ToolCall(
                tool_name="read_section",
                arguments={"doc_id": hit.doc_id, "section_id": hit.section_id},
            ),
            2,
        )
        assert read.status == "ok"
        assert read.retrieval[0].content == hit.content
        assert env.side_effect_for("read_section").value == "read_only"
    snapshot = env.snapshot_state()
    snapshot["docs"].clear()
    assert env.state.docs
    assert "orders" not in env.snapshot_state()


def test_unknown_sections_filters_tools_and_workflows_fail_loudly():
    env = environment()
    assert not env.validate_call(ToolCall(tool_name="issue_refund"))[0]
    assert not env.validate_call(
        ToolCall(tool_name="search_docs", arguments={"query": "policy", "status_filter": "oops"})
    )[0]
    result = env.execute(
        ToolCall(tool_name="read_section", arguments={"doc_id": "missing", "section_id": "no"})
    )
    assert result.status == "error"
    assert result.retrieval is None
    task = env.task.model_copy(update={"workflow_type": "unknown.workflow"})
    with pytest.raises(ValueError, match="unknown workflow_type"):
        create_environment(task)


def test_section_metadata_rejects_corrupt_spans_and_duplicate_ids():
    payload = environment().snapshot_state()
    for mutation in ("outside", "overlap", "fact", "duplicate"):
        state = json.loads(json.dumps(payload))
        sections = state["docs"][0]["metadata"]["sections"]
        if mutation == "outside":
            sections[0]["span_end"] = 10000
        elif mutation == "overlap":
            sections[1]["span_start"] = 0
        elif mutation == "fact":
            sections[0]["facts"]["retention_days"] = "Not in this section."
        else:
            sections[1]["section_id"] = sections[0]["section_id"]
        with pytest.raises(ValidationError):
            RetrievalState.model_validate(state)


@pytest.mark.parametrize("start,end", [(-1, 5), (5, 5), (7, 2), (True, 3), ("0", 3)])
def test_invalid_citation_offsets_are_not_coerced(start, end):
    with pytest.raises(ValidationError):
        Citation(doc_id="doc", section_id="section", span_start=start, span_end=end)


def test_verifier_does_not_accept_blocked_or_missing_answers(suite_runs):
    input = verifier_input(suite_runs)
    for event in input.trace:
        if event.event_type is TraceEventType.FINAL_ANSWER:
            event.payload["blocked_by"] = CONTROL_ID
    result = RetrievalAnswerVerifier().verify(input)
    assert result.verdict is VerifierVerdict.INCOMPLETE
    assert not result.passed
    assert result.warnings


def test_invalid_state_and_missing_expectations_are_incomplete(suite_runs):
    input = verifier_input(suite_runs)
    input.initial_state = {"docs": "not a corpus"}
    assert RetrievalAnswerVerifier().verify(input).verdict is VerifierVerdict.INCOMPLETE
    input = verifier_input(suite_runs)
    input.task.metadata.pop("retrieval_expectation")
    assert RetrievalAnswerVerifier().verify(input).verdict is VerifierVerdict.INCOMPLETE


@pytest.mark.parametrize("sections", [None, 42, "not sections", {"section_id": "retention"}])
def test_malformed_section_metadata_is_incomplete_not_a_crash(suite_runs, sections):
    input = verifier_input(suite_runs)
    input.initial_state["docs"][0]["metadata"]["sections"] = sections
    result = RetrievalAnswerVerifier().verify(input)
    assert result.verdict is VerifierVerdict.INCOMPLETE
    assert result.warnings == ["corpus or retrieval expectation unavailable or invalid"]


@pytest.mark.parametrize("ids", [[""], ["retention_policy_v2", "retention_policy_v2"], ["missing"]])
def test_invalid_expectation_cannot_silently_pass(suite_runs, ids):
    input = verifier_input(suite_runs)
    input.task.metadata["retrieval_expectation"]["relevant_doc_ids"] = ids
    assert RetrievalAnswerVerifier().verify(input).verdict is VerifierVerdict.INCOMPLETE


def test_empty_initial_corpus_is_not_replaced_by_final_corpus(suite_runs):
    input = verifier_input(suite_runs)
    input.initial_state = {"schema_version": "0.1.0", "docs": []}
    assert RetrievalAnswerVerifier().verify(input).verdict is VerifierVerdict.INCOMPLETE


def test_citations_must_have_been_seen_before_the_standing_answer(suite_runs):
    input = verifier_input(suite_runs)
    retrievals = [e for e in input.trace if e.event_type is TraceEventType.RETRIEVAL_RESULT]
    input.trace = [e for e in input.trace if e.event_type is not TraceEventType.RETRIEVAL_RESULT]
    input.trace.extend(retrievals)
    result = RetrievalAnswerVerifier().verify(input)
    assert not result.passed
    assert "retrieval_citation_unsupported" in {c.check_id for c in result.failed_checks}


def test_every_citation_must_support_the_claim_not_just_one(suite_runs):
    input = verifier_input(suite_runs)

    def add_bad_citation(answer):
        answer["claims"][0]["citations"].append(
            {"doc_id": "missing", "section_id": "retention", "span_start": 0, "span_end": 3}
        )

    result = RetrievalAnswerVerifier().verify(rewrite_answer(input, add_bad_citation))
    assert {c.check_id for c in result.failed_checks} == {"retrieval_citation_unsupported"}


def test_known_answer_cannot_evade_completion_by_abstaining(suite_runs):
    input = rewrite_answer(
        verifier_input(suite_runs), lambda a: a.update(claims=[], abstained=True)
    )
    result = RetrievalAnswerVerifier().verify(input)
    assert {c.check_id for c in result.failed_checks} == {"retrieval_answer_incomplete"}


def test_current_citation_passes_even_when_deprecated_evidence_was_retrieved(suite_runs):
    input = verifier_input(suite_runs)
    result = RetrievalAnswerVerifier().verify(input)
    assert result.passed
    assert any(
        item["status"] == "deprecated"
        for event in input.trace
        if event.event_type is TraceEventType.RETRIEVAL_RESULT
        for item in event.payload["results"]
    )


def test_final_state_cannot_rewrite_the_initial_corpus(suite_runs):
    input = verifier_input(suite_runs, "retrieval_unsupported_claim")
    input.final_state["docs"] = []
    result = RetrievalAnswerVerifier().verify(input)
    assert "retrieval_unsupported_claim" in {c.check_id for c in result.failed_checks}


def test_legacy_context_without_an_initial_snapshot_can_use_final_state(suite_runs):
    input = verifier_input(suite_runs)
    input.initial_state = {}
    assert RetrievalAnswerVerifier().verify(input).passed


def test_pipeline_and_cli_verify_use_the_recorded_initial_corpus(tmp_path):
    store = ArtifactStore(tmp_path)
    run = run_task_pipeline(
        TASKS / "retrieval_bad_span_positive.json", AgentConfig(label="fixture"), store
    )
    run_id = run.run_result.run_id
    final = store.read_json(run_id, names.FINAL_STATE)
    final["docs"] = []
    store.write_json(run_id, names.FINAL_STATE, final)
    assert verify_run(store, run.run_result, run.task).passed
    assert main(["--runs-dir", str(tmp_path), "verify", str(store.run_dir(run_id))]) == 0
    assert store.read_json(run_id, names.VERIFIER_RESULT)["passed"]


def test_control_is_narrow_uninstallable_and_workflow_specific():
    env = environment()
    instance = select_controls([CONTROL_ID])[0]
    env.install_control(instance)
    script = json.loads(
        (
            FIXTURES_DIR / "scripts" / "retrieval" / "retrieval_stale_document_script.json"
        ).read_text()
    )
    answer = script["actions"][-1]["final_answer"]
    assert env.check_final_answer(answer) is None  # no current evidence was retrieved yet
    env.execute(ToolCall(tool_name="search_docs", arguments={"query": "retention"}), 1)
    block = env.check_final_answer(answer)
    assert block.blocked_by == CONTROL_ID
    env.uninstall_control(CONTROL_ID)
    assert env.check_final_answer(answer) is None
    with pytest.raises(ValueError, match="belongs to support.refund"):
        env.install_control(select_controls(["ctl_refund_window_v1"])[0])
    # Corpus validation is used by the tools and the oracle alike.
    assert sections_for(env.state.docs[0])


def test_control_acceptance_requires_an_explicit_continuation(tmp_path, capsys):
    store = ArtifactStore(tmp_path)
    run = run_task_pipeline(
        TASKS / "retrieval_stale_document.json", AgentConfig(label="fixture"), store
    )
    artifact = store.artifact_path(run.run_result.run_id, names.REGRESSION_ARTIFACT)
    command = [
        "--runs-dir",
        str(tmp_path),
        "replay",
        str(artifact),
        "--apply-control",
        "--control",
        CONTROL_ID,
        "--fail-on-rejected",
    ]
    assert main(command) == 1
    validation = store.read_json(run.run_result.run_id, names.REPAIR_VALIDATION)
    rejected_recording = next(v for v in validation["controls"] if v["control_id"] == CONTROL_ID)
    assert rejected_recording["verdict"] != "accepted"
    assert main([*command, "--continuation-script", str(RECOVERY)]) == 0
    validation = store.read_json(run.run_result.run_id, names.REPAIR_VALIDATION)
    accepted = next(v for v in validation["controls"] if v["control_id"] == CONTROL_ID)
    assert accepted["verdict"] == "accepted"
    assert accepted["standing"] == "advisory"
    assert not accepted["label_supported"]
    assert all(s["verdict"] == "PASS" for s in accepted["sibling_reruns"])
    basis = store.read_json(run.run_result.run_id, names.REGRESSION_ARTIFACT)["replay_mode_basis"]
    assert basis["control_ids"] == [CONTROL_ID]
    assert basis["control_step"] == 3
    assert basis["gated_tool"] == "final_answer"
    assert basis["checks_covered_by_control"] == ["retrieval_stale_document"]
    rerun_id = accepted["originating_rerun"]["run_id"]
    config = store.read_json(rerun_id, names.RUN_CONFIG)
    assert config["metadata"]["replay_continuation"]["task_id"] == run.task.task_id
    trace = store.read_trace(rerun_id)
    answers = [e for e in trace if e.event_type is TraceEventType.FINAL_ANSWER]
    assert len(answers) == 2
    assert answers[0].payload["blocked_by"] == CONTROL_ID
    assert answers[-1].payload.get("blocked_by") is None
    assert RetrievalAnswer.model_validate_json(answers[-1].payload["final_answer"]).claims
    assert "advisory" in capsys.readouterr().out


def test_default_replay_control_belongs_to_retrieval(tmp_path):
    store = ArtifactStore(tmp_path)
    run = run_task_pipeline(
        TASKS / "retrieval_stale_document.json", AgentConfig(label="fixture"), store
    )
    artifact = store.artifact_path(run.run_result.run_id, names.REGRESSION_ARTIFACT)
    assert (
        main(
            [
                "--runs-dir",
                str(tmp_path),
                "replay",
                str(artifact),
                "--apply-control",
                "--continuation-script",
                str(RECOVERY),
            ]
        )
        == 0
    )
    validation = store.read_json(run.run_result.run_id, names.REPAIR_VALIDATION)
    assert validation["controls"][0]["control_id"] == CONTROL_ID
    assert validation["controls"][0]["verdict"] == "accepted"


@pytest.mark.parametrize("extra", [[], ["--apply-control", "--commit"]])
def test_continuation_cannot_be_used_as_plain_replay_or_promoted(extra, tmp_path):
    assert (
        main(
            [
                "--runs-dir",
                str(tmp_path),
                "replay",
                "nonexistent.json",
                "--continuation-script",
                str(RECOVERY),
                *extra,
            ]
        )
        == 2
    )


def test_continuation_must_name_the_originating_task(suite_runs, tmp_path):
    store, runs = suite_runs
    artifact = store.artifact_path(runs["retrieval_stale_document"], names.REGRESSION_ARTIFACT)
    continuation = json.loads(RECOVERY.read_text())
    continuation["task_id"] = "different_task"
    path = tmp_path / "mismatched.json"
    path.write_text(json.dumps(continuation))
    assert (
        main(
            [
                "--runs-dir",
                str(tmp_path),
                "replay",
                str(artifact),
                "--apply-control",
                "--continuation-script",
                str(path),
            ]
        )
        == 2
    )
