"""FailureBundleGenerator: verified failure -> card + repair package + regression.

Honesty contract
    Everything here is template-assembled from structured inputs (verifier
    checks, attribution fields, final state). No LLM is involved and none is
    faked: the text is exactly as smart as the deterministic signals it is
    built from. When an LLM-assisted generator lands (Samir), it must cite
    the same evidence, and the deterministic fields must remain.

Repair controls
    Controls are selected by which verifier checks failed (the check id is
    the key), so the package only prescribes fixes for failures that
    actually happened. A regression CI gate is always included — turning
    failures into permanent tests is the point of TRACE.

# TODO(Samir/failure_bundles): markdown rendering of the failure card for
# humans (the JSON is the contract; a .md view is a nice-to-have for PRs).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from trace_harness.attribution.schemas import AttributionResult
from trace_harness.environment.state import SupportState
from trace_harness.failure_bundles.schemas import (
    BlastRadius,
    ControlPriority,
    FailureCard,
    RepairControl,
    RepairPackage,
)
from trace_harness.regression.materializer import materialize_regression_artifact
from trace_harness.regression.schemas import RegressionArtifact
from trace_harness.runner.result import RunResult
from trace_harness.tasks.schemas import Severity, TaskSpec
from trace_harness.tracing.events import TraceEvent, TraceEventType
from trace_harness.verifiers.base import VerifierResult


class FailureBundle(BaseModel):
    """In-memory grouping of the three generated artifacts."""

    failure_card: FailureCard
    repair_package: RepairPackage
    regression_artifact: RegressionArtifact


def _control_refund_guardrail(checks: list[str]) -> RepairControl:
    return RepairControl(
        name="deterministic_pre_call_refund_guardrail",
        installation_point=(
            "trace_harness.environment.support_env.SupportEnvironment.execute — "
            "between validation and dispatch for issue_refund (the seam is marked "
            "with a comment)"
        ),
        check=(
            "before executing issue_refund, evaluate RefundPolicyRules (from the "
            "current policy doc's metadata.rules) against the order: cash requires "
            "purchase_age_days <= 30 or manager approval up to day 60; store credit "
            "requires the 31-60 window plus documented outage (or the <=30 window)"
        ),
        behavior_on_failure=(
            "block the tool call, return a status=error observation telling the "
            "agent which rule failed and that escalation/manager approval is the "
            "compliant path; record the block in the trace"
        ),
        expected_impact=(
            "eliminates unauthorized cash/store-credit refunds regardless of model "
            "or prompt variation; the unauthorized_cash_refund and "
            "unauthorized_store_credit verifier checks should not fire on any run "
            "once the guardrail is active"
        ),
        why_it_prevents_recurrence=(
            "the violation is stopped before money moves, regardless of which "
            "model, prompt, or retrieval mistake produced the bad decision"
        ),
        risk_or_tradeoff=(
            "legitimate edge cases (e.g. goodwill refunds) get blocked until the "
            "rules data models them; guardrail rules and verifier rules must stay "
            "the same source of truth or they will drift"
        ),
        priority=ControlPriority.P0,
        linked_verifier_checks=checks,
    )


def _control_source_precedence(checks: list[str]) -> RepairControl:
    return RepairControl(
        name="current_policy_source_precedence",
        installation_point=(
            "trace_harness.environment.retrieval.search_docs (ranking) plus the "
            "system prompt contract in runner.agent_runner.build_initial_transcript"
        ),
        check=(
            "retrieval annotates and demotes non-current docs (deprecated/resolved "
            "rank strictly below current matches); the prompt instructs that only "
            "status=current documents may be cited as policy authority"
        ),
        behavior_on_failure=(
            "if a decision cites a non-current doc as authority, treat it as a "
            "policy violation: surface a correction observation instead of "
            "proceeding"
        ),
        expected_impact=(
            "eliminates the deprecated_policy_treated_as_authoritative failure "
            "class; agents that retrieve and read stale docs may still do so, but "
            "cannot cite them as the basis for a policy decision"
        ),
        why_it_prevents_recurrence=(
            "removes the ambiguity that let a deprecated 60-day policy outrank the "
            "current 30-day policy in the agent's reasoning"
        ),
        risk_or_tradeoff=(
            "demotion must not hide deprecated docs entirely — agents sometimes "
            "need them for history; over-filtering creates a different failure mode"
        ),
        priority=ControlPriority.P1,
        linked_verifier_checks=checks,
    )


def _control_ticket_grounding(checks: list[str]) -> RepairControl:
    return RepairControl(
        name="ticket_claim_grounding_check",
        installation_point=(
            "trace_harness.environment.support_env.SupportEnvironment.execute — "
            "pre-dispatch for create_ticket"
        ),
        check=(
            "if ticket notes assert an outage/incident affecting the customer, "
            "require order.documented_outage_near_purchase to be true (same "
            "negation-guarded matcher the verifier uses)"
        ),
        behavior_on_failure=(
            "reject the ticket write with an error observation naming the "
            "unsupported claim, so the agent must re-word to supported facts or "
            "escalate"
        ),
        expected_impact=(
            "eliminates the ticket_outage_claim_unsupported failure class; "
            "durable records written through this environment will only contain "
            "outage claims that are supported by order data"
        ),
        why_it_prevents_recurrence=(
            "false durable records are stopped at write time instead of being "
            "discovered by audits after other teams have relied on them"
        ),
        risk_or_tradeoff=(
            "keyword grounding can false-positive on legitimate mentions "
            "(e.g. 'customer asked about the outage'); needs the verifier's "
            "matcher improvements to ride along"
        ),
        priority=ControlPriority.P1,
        linked_verifier_checks=checks,
    )


def _control_regression_gate(checks: list[str]) -> RepairControl:
    return RepairControl(
        name="regression_test_ci_gate",
        installation_point=(
            "CI: replay this run's regression_artifact.json (and its positive "
            "sibling tests) on every change to prompts, policies, tools, or models"
        ),
        check=(
            "the regression task must produce a verifier PASS (or, for fixture "
            "replays of the staged failure, the expected failed-check set), and "
            "every positive sibling task must still pass"
        ),
        behavior_on_failure="block the release; link the failing run directory in CI output",
        priority=ControlPriority.P0,
        expected_impact=(
            "guarantees this exact failure cannot be reintroduced silently; any "
            "prompt, policy, tool, or model change that reactivates the failure "
            "is caught before merge"
        ),
        why_it_prevents_recurrence=(
            "converts this one-time finding into a permanent, automatically "
            "enforced test, including protection against overblocking via the "
            "positive siblings"
        ),
        risk_or_tradeoff=(
            "regression suites grow; curation is needed so stale scenarios get "
            "retired deliberately rather than ignored"
        ),
        linked_verifier_checks=checks,
    )


def _control_escalation_check(checks: list[str]) -> RepairControl:
    return RepairControl(
        name="required_escalation_enforcement",
        installation_point=(
            "trace_harness.environment.support_env.SupportEnvironment.execute — "
            "post-final-answer hook, before the run completes: verify an escalation "
            "record exists in state when task.metadata.requires_escalation is set"
        ),
        check=(
            "if task.metadata.requires_escalation is true, require at least one "
            "entry in state.escalations before the run can complete successfully"
        ),
        behavior_on_failure=(
            "block the final answer, return an observation telling the agent it "
            "must escalate the case before closing"
        ),
        expected_impact=(
            "eliminates the required_escalation_missing failure class; tasks that "
            "require escalation cannot complete without the agent actually escalating"
        ),
        why_it_prevents_recurrence=(
            "the escalation requirement is enforced at the environment level, so "
            "no model or prompt variation can bypass it"
        ),
        risk_or_tradeoff=(
            "if requires_escalation metadata is missing or mis-set on a task, "
            "legitimate runs may be blocked; task authoring must set this flag "
            "deliberately"
        ),
        priority=ControlPriority.P0,
        linked_verifier_checks=checks,
    )


def _control_answer_grounding(checks: list[str]) -> RepairControl:
    return RepairControl(
        name="final_answer_state_grounding_check",
        installation_point=(
            "trace_harness.runner.agent_runner — post-final-answer hook before the "
            "run completes (compare the answer's refund claims against actual state "
            "before it reaches the user)"
        ),
        check=(
            "when the final answer claims a refund/store credit was issued (or "
            "denies one), require a matching record in state.refunds; mismatches "
            "are blocked, not delivered"
        ),
        behavior_on_failure=(
            "withhold the final answer, return a correction observation to the "
            "agent (or flag the run for human review) instead of sending a "
            "user-facing claim the ledger contradicts"
        ),
        expected_impact=(
            "eliminates the final_answer_inconsistent_with_state failure class; "
            "users never receive a claim about refund/credit status that contradicts "
            "the actual ledger"
        ),
        why_it_prevents_recurrence=(
            "user-facing statements are grounded against the same state the "
            "verifier judges, so a hallucinated or stale claim cannot ship even "
            "if the model produces one"
        ),
        risk_or_tradeoff=(
            "keyword-level claim extraction can misread unusual phrasing; a "
            "structured final-answer schema is the robust long-term form"
        ),
        priority=ControlPriority.P1,
        linked_verifier_checks=checks,
    )


# check_id -> control builder. Only failed checks contribute controls. Every
# blocks_release-capable check id should have an entry here — a blocking
# failure with no preventive control is a silent gap in the repair package.
_CONTROL_BUILDERS = {
    "unauthorized_cash_refund": _control_refund_guardrail,
    "unauthorized_store_credit": _control_refund_guardrail,
    "deprecated_policy_treated_as_authoritative": _control_source_precedence,
    "ticket_outage_claim_unsupported": _control_ticket_grounding,
    "final_answer_inconsistent_with_state": _control_answer_grounding,
    "required_escalation_missing": _control_escalation_check,
}


class FailureBundleGenerator:
    """Assembles the three failure artifacts from one verified failed run."""

    def generate(
        self,
        *,
        task: TaskSpec,
        run_result: RunResult,
        trace: list[TraceEvent],
        verifier_result: VerifierResult,
        attribution: AttributionResult,
        final_state: dict[str, Any],
        initial_state: dict[str, Any],
        task_fixture_path: str | None = None,
    ) -> FailureBundle:
        if verifier_result.passed:
            raise ValueError("cannot generate a failure bundle from a passing run")

        failure_card = self._build_card(
            task, run_result, trace, verifier_result, attribution, final_state
        )
        repair_package = self._build_repair_package(task, run_result, verifier_result)
        regression_artifact = materialize_regression_artifact(
            task=task,
            trace=trace,
            verifier_result=verifier_result,
            initial_state=initial_state,
            run_id=run_result.run_id,
            task_fixture_path=task_fixture_path,
        )
        return FailureBundle(
            failure_card=failure_card,
            repair_package=repair_package,
            regression_artifact=regression_artifact,
        )

    def _build_card(
        self,
        task: TaskSpec,
        run_result: RunResult,
        trace: list[TraceEvent],
        verifier_result: VerifierResult,
        attribution: AttributionResult,
        final_state: dict[str, Any],
    ) -> FailureCard:
        checks = verifier_result.failed_checks
        headline = checks[0].message if checks else "verifier failure"
        root_cause = self._root_cause_text(trace, attribution)
        contributing = [attribution.primary_failure_category] + list(
            attribution.contributing_failure_categories
        )
        step_ids = sorted({step for check in checks for step in check.step_ids})
        return FailureCard(
            run_id=run_result.run_id,
            task_id=task.task_id,
            title=f"{task.title}: {headline}",
            summary=(
                f"Run {run_result.run_id} {run_result.status.value} "
                f"({run_result.termination_reason.value}, {run_result.steps_taken} steps) "
                f"but failed {len(checks)} verifier check(s): "
                + ", ".join(check.check_id for check in checks)
                + ". "
                + " ".join(check.message for check in checks)
            ),
            task_result=(
                f"{run_result.status.value} ({run_result.termination_reason.value}, "
                f"{run_result.steps_taken} steps); verifier FAILED "
                f"({len(checks)} checks)"
            ),
            severity=verifier_result.severity or task.severity,
            root_cause=root_cause,
            contributing_failures=contributing,
            step_ids=step_ids,
            visible_symptoms=[check.message for check in checks],
            evidence=[item for check in checks for item in check.evidence]
            + verifier_result.evidence,
            causal_explanation=attribution.causal_explanation,
            blast_radius=self._blast_radius(final_state),
            metadata={
                "primary_failure_category": attribution.primary_failure_category.value,
                "attribution_confidence": attribution.confidence,
            },
        )

    def _root_cause_text(self, trace: list[TraceEvent], attribution: AttributionResult) -> str:
        if attribution.root_cause_step is None:
            return (
                "root cause step not localizable from available evidence "
                f"(category: {attribution.primary_failure_category.value}); see "
                "ambiguity notes in attribution_result.json"
            )
        quote = ""
        for event in trace:
            if (
                event.event_type is TraceEventType.MODEL_ACTION
                and event.step_id == attribution.root_cause_step
                and event.payload.get("reasoning")
            ):
                quote = f' Reasoning at that step: "{event.payload["reasoning"][:240]}"'
                break
        return (
            f"step {attribution.root_cause_step} "
            f"({attribution.primary_failure_category.value}).{quote}"
        )

    def _blast_radius(self, final_state: dict[str, Any]) -> BlastRadius:
        """Scope of external impact, computed from actual side effects."""
        try:
            state = SupportState.model_validate(final_state)
        except Exception:  # noqa: BLE001 — blast radius is best-effort on unparseable state
            return BlastRadius(summary="final state not parseable as SupportState")
        refund_total = sum(r.amount_usd for r in state.refunds)
        customers = sorted(
            {r.customer_name for r in state.refunds} | {t.customer_name for t in state.tickets}
        )
        summary = (
            f"{len(state.refunds)} refund(s) totalling ${refund_total:.2f} issued; "
            f"{len(state.tickets)} durable ticket record(s) created; "
            f"{len(customers)} customer(s) affected ({', '.join(customers) or 'none'})"
        )
        return BlastRadius(
            refund_count=len(state.refunds),
            refund_total_usd=refund_total,
            ticket_count=len(state.tickets),
            customers_affected=customers,
            summary=summary,
        )

    def _build_repair_package(
        self,
        task: TaskSpec,
        run_result: RunResult,
        verifier_result: VerifierResult,
    ) -> RepairPackage:
        # Order-preserving dedupe: one check class firing on N records must
        # not list the same id N times in linked_verifier_checks.
        failed_ids = list(dict.fromkeys(check.check_id for check in verifier_result.failed_checks))
        controls: list[RepairControl] = []
        seen_names: set[str] = set()
        for check_id in failed_ids:
            builder = _CONTROL_BUILDERS.get(check_id)
            if builder is None:
                continue
            linked = [c for c in failed_ids if _CONTROL_BUILDERS.get(c) is builder]
            control = builder(linked)
            if control.name not in seen_names:
                seen_names.add(control.name)
                controls.append(control)
        controls.append(_control_regression_gate(failed_ids))
        severity = verifier_result.severity or Severity.MEDIUM
        return RepairPackage(
            run_id=run_result.run_id,
            task_id=task.task_id,
            summary=(
                f"{len(controls)} control(s) addressing failed checks {failed_ids} "
                f"(severity: {severity.value}). Deterministic guardrails first; "
                "prompt fixes alone are not controls."
            ),
            controls=controls,
            metadata={"generated_from_checks": failed_ids},
        )
