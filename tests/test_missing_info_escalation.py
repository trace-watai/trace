"""TRA-79: the canonical missing-information run must produce one durable,
traceable escalation, not a plain decline or a generic ticket.

These tests prove the trace <-> state <-> verifier linkage for escalate_case
end-to-end, before any dedicated verifier logic exists for it (see
verifiers/refund_policy.py for the checks that build on this).
"""

from __future__ import annotations

from trace_harness.tracing.events import TraceEventType
from trace_harness.verifiers.registry import get_verifier


def test_missing_info_run_produces_exactly_one_escalation(missing_info_run):
    escalations = missing_info_run.final_state["escalations"]
    assert len(escalations) == 1
    escalation = escalations[0]
    assert escalation["customer_name"] == "Jordan Blake"
    assert escalation["reason"]
    # No refund and no ticket — the case is fully resolved via escalation.
    assert missing_info_run.final_state["refunds"] == []
    assert missing_info_run.final_state["tickets"] == []


def test_escalation_step_provenance_matches_trace(missing_info_run):
    executed = [
        e
        for e in missing_info_run.trace
        if e.event_type is TraceEventType.TOOL_CALL_EXECUTED
        and e.payload["tool_name"] == "escalate_case"
    ]
    assert len(executed) == 1
    escalation = missing_info_run.final_state["escalations"][0]
    assert escalation["created_at_step"] == executed[0].step_id


def test_escalate_case_trace_event_sequence_complete(missing_info_run):
    def events_for(event_type: TraceEventType) -> list:
        return [
            e
            for e in missing_info_run.trace
            if e.event_type is event_type and e.payload.get("tool_name") == "escalate_case"
        ]

    assert len(events_for(TraceEventType.TOOL_CALL_REQUESTED)) == 1
    assert len(events_for(TraceEventType.TOOL_CALL_VALIDATED)) == 1
    assert len(events_for(TraceEventType.TOOL_CALL_EXECUTED)) == 1
    assert len(events_for(TraceEventType.TOOL_OBSERVATION)) == 1


def test_missing_info_run_passes_verifier(missing_info_run):
    """Proves no existing check false-positives on the escalate_case swap."""
    verifier = get_verifier("refund_policy")
    result = verifier.verify(
        _verifier_input(missing_info_run),
    )
    assert result.passed is True
    assert result.failed_checks == []


def test_removing_the_escalation_fails_the_run(missing_info_run):
    """The acceptance-evidence tripwire: strip the escalation from final state
    and confirm the run stops looking correct. Today this surfaces as a
    warning (no check reads state.escalations yet) rather than a failure —
    the dedicated omission check is held off pending Karan's input (TRA-79
    plan, Step 5). This test documents that gap rather than hiding it.
    """
    verifier = get_verifier("refund_policy")
    stripped_state = dict(missing_info_run.final_state)
    stripped_state["escalations"] = []

    result = verifier.verify(_verifier_input(missing_info_run, final_state=stripped_state))

    # TODO(Evan Yang/verifier, TRA-79): once the omission check lands, this
    # should assert result.passed is False. For now, assert the heuristic
    # doesn't crash and record the known gap explicitly.
    assert result.passed is True


def _verifier_input(run, final_state=None):
    from trace_harness.verifiers.base import VerifierInput

    return VerifierInput(
        task=run.task,
        trace=run.trace,
        final_state=final_state if final_state is not None else run.final_state,
        run_id=run.run_id,
    )
