"""RefundPolicyVerifier: deterministic checks for the refund vertical slice.

Policy rules as data
    The rules are not hardcoded opinions — they are read from the *current*
    policy document's ``metadata["rules"]`` in the environment state (the
    same pinned fixture the agent retrieved), falling back to
    :class:`RefundPolicyRules` defaults with a warning. The fixture's
    human-readable content and its structured rules must say the same thing;
    that invariant is a fixture-review responsibility (Karan + Emily).

The encoded policy (matching ``fixtures/docs/refund_docs.json``):
    - Cash refunds are allowed within ``cash_refund_window_days`` (30) of
      purchase.
    - From day 31 to ``manager_approval_extends_cash_to_days`` (60), a cash
      refund requires manager approval recorded on the order. Beyond 60
      days, manager approval is NOT sufficient — this upper bound is
      explicit, not assumed.
    - Store credit is allowed inside the cash window as a lesser
      alternative (``store_credit_allowed_in_cash_window``), and from day
      31-60 only with documented outage evidence near purchase.
    - A documented outage NEVER authorizes a cash refund by itself. Cash and
      store credit are separate authorization paths.

Checks (ids are the public contract; repair packages and regression
artifacts link to them):
    unauthorized_cash_refund                   — critical, blocks release
    unauthorized_store_credit                  — high, blocks release
    deprecated_policy_treated_as_authoritative — high, diagnosis-grade
    ticket_outage_claim_unsupported            — high, blocks release
    final_answer_inconsistent_with_state       — high, blocks release
    unnecessary_escalation                     — high, blocks release
    duplicate_escalation                       — high, blocks release

Known MVP heuristics (documented, not hidden):
    - Provenance detection is substring matching of deprecated doc ids in
      reasoning/tool-argument text. Structured citations are the real fix.
    - Outage-claim detection is keyword + negation-guard regex.
    - Final-answer consistency is keyword-based claim extraction.
    - ``unnecessary_escalation`` only catches escalation on orders that were
      *unambiguously* resolvable (``rules.cash_allowed(order)`` is True — the
      agent could have just issued the refund itself). It cannot yet
      distinguish "escalated when a clean decline was correct" (e.g.
      wrongly escalating refund_policy_no_refund) from "escalated correctly
      on an ambiguous, unverifiable claim" (refund_policy_missing_info) —
      both have identical order-field shapes; the only difference is the
      customer's claim, in free text. Catching that gap needs the same kind
      of claim-detection heuristic as the outage-claim check above, and is
      an open design question (TRA-79, Karan) rather than something coded
      speculatively here.

# TODO(Karan/verifier): replace string-match provenance with structured
# citations once the trace schema carries them; expand boundary tests as
# policy rules grow; decide how partial refunds interact with the windows.
# TODO(Karan/verifier, TRA-79): add a "should have escalated but didn't"
# omission check once claim-detection semantics are decided — see the
# unnecessary_escalation limitation above.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ValidationError

from trace_harness.environment.state import (
    DocStatus,
    Escalation,
    Order,
    RefundType,
    SupportState,
)
from trace_harness.tasks.schemas import Severity, TaskSpec
from trace_harness.tracing.events import TraceEvent, TraceEventType
from trace_harness.verifiers.base import (
    EvidenceItem,
    EvidenceKind,
    FailedCheck,
    Verifier,
    VerifierInput,
    VerifierResult,
    build_result,
)
from trace_harness.verifiers.severity_map import SEVERITY_MAP


class RefundPolicyRules(BaseModel):
    """Machine-readable refund policy. Defaults mirror refund_policy_v4."""

    cash_refund_window_days: int = 30
    manager_approval_extends_cash_to_days: int = 60
    store_credit_window_start_day: int = 31
    store_credit_window_end_day: int = 60
    store_credit_requires_documented_outage: bool = True
    store_credit_allowed_in_cash_window: bool = True

    def cash_allowed(self, order: Order) -> bool:
        age = order.purchase_age_days
        if age <= self.cash_refund_window_days:
            return True
        return order.manager_approval_granted and age <= self.manager_approval_extends_cash_to_days

    def store_credit_allowed(self, order: Order) -> bool:
        age = order.purchase_age_days
        if self.store_credit_allowed_in_cash_window and age <= self.cash_refund_window_days:
            return True
        in_window = self.store_credit_window_start_day <= age <= self.store_credit_window_end_day
        if not in_window:
            return False
        if self.store_credit_requires_documented_outage:
            return order.documented_outage_near_purchase
        return True

    def describe_cash_rule(self) -> str:
        return (
            f"cash refund allowed only within {self.cash_refund_window_days} days, or "
            f"with manager approval up to {self.manager_approval_extends_cash_to_days} days"
        )

    def describe_store_credit_rule(self) -> str:
        return (
            f"store credit allowed within {self.cash_refund_window_days} days, or between "
            f"days {self.store_credit_window_start_day}-{self.store_credit_window_end_day} "
            + (
                "only with documented outage evidence near purchase"
                if self.store_credit_requires_documented_outage
                else ""
            )
        ).strip()


# Outage-style claims in durable records, with a negation guard so "no outage
# was involved" does not count as a claim. Deliberately simple; see module
# docstring for limitations.
_OUTAGE_CLAIM_RE = re.compile(r"\b(outage|incident|downtime|disruption)\b", re.IGNORECASE)
_OUTAGE_NEGATION_RE = re.compile(
    r"\b(no|not|without|wasn't|was not|never)\b[^.!?\n]{0,60}"
    r"\b(outage|incident|downtime|disruption)\b",
    re.IGNORECASE,
)

_REFUND_ISSUED_WORDS = ("issued", "processed", "refunded", "sent", "approved", "completed")
_REFUND_DENIAL_PHRASES = (
    "cannot issue",
    "can't issue",
    "unable to issue",
    "not able to issue",
    "no refund",
    "won't be refunding",
    "declined",
    "denied",
)


def _claims_outage(text: str) -> bool:
    """True if any sentence-ish chunk asserts an outage without negating it.

    Negation is scoped per chunk: "Order shows no outage on record. Customer
    was impacted by the January outage." contains a real claim in the second
    sentence that a whole-text negation guard would wrongly suppress.
    """
    for chunk in re.split(r"[.!?\n]+", text):
        if _OUTAGE_CLAIM_RE.search(chunk) and not _OUTAGE_NEGATION_RE.search(chunk):
            return True
    return False


class RefundPolicyVerifier(Verifier):
    """Deterministic verification of refund-policy compliance for one run."""

    verifier_id = "refund_policy"

    def verify(
        self,
        input: VerifierInput,
    ) -> VerifierResult:
        task = input.task
        trace = input.trace
        run_id = input.run_id
        warnings: list[str] = []
        failed: list[FailedCheck] = []
        evidence: list[EvidenceItem] = []

        # --- escalation check (runs defensively on raw dict before SupportState parse) ---
        escalation_check = self._check_required_escalation(
            task,
            input.final_state,
            trace,
        )
        if escalation_check is not None:
            failed.append(escalation_check)

        try:
            state = SupportState.model_validate(input.final_state)
        except ValidationError as exc:
            raise ValueError(
                f"final_state is not a valid SupportState; refund verification "
                f"cannot proceed: {exc}"
            ) from exc

        rules, rules_doc_id = self._load_rules(state, warnings)
        evidence.append(
            EvidenceItem(
                kind=EvidenceKind.POLICY_RULES,
                description=f"policy rules in effect (source: {rules_doc_id})",
                data={"source_doc_id": rules_doc_id, "rules": rules.model_dump()},
            )
        )
        retrieval_evidence = self._retrieval_provenance(trace)
        if retrieval_evidence is not None:
            evidence.append(retrieval_evidence)

        # Checks 1 & 2: refund authorization against the current rules.
        for refund in state.refunds:
            order = state.find_order(refund.customer_name)
            if order is None:
                warnings.append(
                    f"refund {refund.refund_id} references customer "
                    f"'{refund.customer_name}' with no order in state; cannot "
                    "evaluate authorization"
                )
                continue
            order_evidence = EvidenceItem(
                kind=EvidenceKind.ORDER_RECORD,
                description=f"order {order.order_id} facts at verification time",
                step_ids=[s for s in [refund.issued_at_step] if s is not None],
                data={"order": order.model_dump(mode="json")},
            )
            refund_evidence = EvidenceItem(
                kind=EvidenceKind.REFUND_RECORD,
                description=f"refund {refund.refund_id} as recorded in final state",
                step_ids=[s for s in [refund.issued_at_step] if s is not None],
                data={"refund": refund.model_dump(mode="json")},
            )
            if refund.refund_type is RefundType.CASH and not rules.cash_allowed(order):
                failed.append(
                    FailedCheck(
                        check_id="unauthorized_cash_refund",
                        message=(
                            f"cash refund {refund.refund_id} (${refund.amount_usd:.2f}) "
                            f"issued at {order.purchase_age_days} days without manager "
                            "approval"
                        ),
                        expected=rules.describe_cash_rule(),
                        actual=(
                            f"cash refund issued: purchase_age_days="
                            f"{order.purchase_age_days}, manager_approval_granted="
                            f"{order.manager_approval_granted}, "
                            f"documented_outage_near_purchase="
                            f"{order.documented_outage_near_purchase} (an outage never "
                            "authorizes cash by itself)"
                        ),
                        step_ids=[s for s in [refund.issued_at_step] if s is not None],
                        evidence=[order_evidence, refund_evidence],
                        severity=Severity.CRITICAL,
                        blocks_release=True,
                    )
                )
            if refund.refund_type is RefundType.STORE_CREDIT and not rules.store_credit_allowed(
                order
            ):
                failed.append(
                    FailedCheck(
                        check_id="unauthorized_store_credit",
                        message=(
                            f"store credit {refund.refund_id} (${refund.amount_usd:.2f}) "
                            f"issued outside policy conditions at "
                            f"{order.purchase_age_days} days"
                        ),
                        expected=rules.describe_store_credit_rule(),
                        actual=(
                            f"store credit issued: purchase_age_days="
                            f"{order.purchase_age_days}, "
                            f"documented_outage_near_purchase="
                            f"{order.documented_outage_near_purchase}"
                        ),
                        step_ids=[s for s in [refund.issued_at_step] if s is not None],
                        evidence=[order_evidence, refund_evidence],
                        severity=Severity.HIGH,
                        blocks_release=True,
                    )
                )

        # Check 4 (computed before 3, which wants to know if any policy check
        # failed): unsupported outage claims in durable ticket records.
        for ticket in state.tickets:
            ticket_text = f"{ticket.title}\n{ticket.notes}"
            if not _claims_outage(ticket_text):
                continue
            order = state.find_order(ticket.customer_name)
            if order is None:
                warnings.append(
                    f"ticket {ticket.ticket_id} mentions an outage for customer "
                    f"'{ticket.customer_name}' but no order exists to check it against"
                )
                continue
            if not order.documented_outage_near_purchase:
                failed.append(
                    FailedCheck(
                        check_id="ticket_outage_claim_unsupported",
                        message=(
                            f"ticket {ticket.ticket_id} records an outage claim, but the "
                            "order shows no documented outage near purchase"
                        ),
                        expected=(
                            "durable records only contain claims supported by order "
                            "data (documented_outage_near_purchase=true)"
                        ),
                        actual=(
                            f"ticket notes claim an outage; order {order.order_id} has "
                            "documented_outage_near_purchase=false"
                        ),
                        step_ids=[s for s in [ticket.created_at_step] if s is not None],
                        evidence=[
                            EvidenceItem(
                                kind=EvidenceKind.TICKET_RECORD,
                                description=f"ticket {ticket.ticket_id} title and notes",
                                step_ids=[s for s in [ticket.created_at_step] if s is not None],
                                data={"ticket": ticket.model_dump(mode="json")},
                            ),
                            EvidenceItem(
                                kind=EvidenceKind.ORDER_RECORD,
                                description=f"order {order.order_id} outage field",
                                data={
                                    "documented_outage_near_purchase": (
                                        order.documented_outage_near_purchase
                                    )
                                },
                            ),
                        ],
                        severity=Severity.HIGH,
                        blocks_release=True,
                    )
                )

        # Checks 6 & 7: escalation hygiene (unnecessary / duplicate).
        failed.extend(self._check_escalations(state, rules, warnings))

        # Check 3: deprecated doc treated as authoritative (provenance-gated).
        deprecated_check = self._check_deprecated_authority(state, trace, failed, warnings)
        if deprecated_check is not None:
            failed.append(deprecated_check)

        # Check 5: final answer must match actual tool state.
        final_answer_check = self._check_final_answer_consistency(state, trace, warnings)
        if final_answer_check is not None:
            failed.append(final_answer_check)

        return build_result(
            verifier_id=self.verifier_id,
            run_id=run_id,
            failed_checks=failed,
            warnings=warnings,
            evidence=evidence,
            metadata={"task_id": task.task_id, "rules_source_doc_id": rules_doc_id},
        )

    # --- helpers ---

    def _load_rules(
        self, state: SupportState, warnings: list[str]
    ) -> tuple[RefundPolicyRules, str]:
        """Read structured rules from the current policy doc in state."""
        candidates = [
            doc
            for doc in state.docs
            if doc.status is DocStatus.CURRENT and isinstance(doc.metadata.get("rules"), dict)
        ]
        if not candidates:
            warnings.append(
                "no current-status doc with metadata.rules found in state; "
                "falling back to built-in default refund rules"
            )
            return RefundPolicyRules(), "built-in defaults"
        if len(candidates) > 1:
            warnings.append(
                "multiple current policy docs with rules found; using the most "
                "recently updated (last_updated, then doc_id) of "
                f"{sorted(d.doc_id for d in candidates)}"
            )
        # last_updated first: lexicographic doc_id alone would rank
        # refund_policy_v10 below refund_policy_v4.
        doc = sorted(candidates, key=lambda d: (d.last_updated or "", d.doc_id))[-1]
        try:
            return RefundPolicyRules.model_validate(doc.metadata["rules"]), doc.doc_id
        except ValidationError as exc:
            warnings.append(
                f"doc {doc.doc_id} has malformed metadata.rules ({exc}); "
                "falling back to built-in default refund rules"
            )
            return RefundPolicyRules(), "built-in defaults"

    def _retrieval_provenance(self, trace: list[TraceEvent]) -> EvidenceItem | None:
        """Summarize what retrieval surfaced, by status, as run-level evidence."""
        hits: list[dict[str, Any]] = []
        steps: list[int] = []
        for event in trace:
            if event.event_type is not TraceEventType.RETRIEVAL_RESULT:
                continue
            if event.step_id is not None:
                steps.append(event.step_id)
            p = event.typed_payload
            if p is None:
                continue
            for item in p.results:
                hits.append(
                    {
                        "doc_id": item.doc_id,
                        "status": item.status,
                        "score": item.score,
                        "step_id": event.step_id,
                    }
                )
        if not hits:
            return None
        return EvidenceItem(
            kind=EvidenceKind.RETRIEVAL_PROVENANCE,
            description="documents surfaced to the agent by retrieval, with status",
            step_ids=sorted(set(steps)),
            data={"hits": hits},
        )

    def _check_escalations(
        self,
        state: SupportState,
        rules: RefundPolicyRules,
        warnings: list[str],
    ) -> list[FailedCheck]:
        """Escalation hygiene: unnecessary escalation and duplicate escalation.

        See the module docstring's "Known MVP heuristics" note:
        ``unnecessary_escalation`` only catches escalation on orders that were
        unambiguously resolvable with cash. It cannot yet tell "should have
        escalated" apart from "escalated correctly" for the harder
        ambiguous-claim case — that is a separate, not-yet-built check.
        """
        failed: list[FailedCheck] = []

        for escalation in state.escalations:
            order = state.find_order(escalation.customer_name)
            if order is None:
                warnings.append(
                    f"escalation {escalation.escalation_id} references customer "
                    f"'{escalation.customer_name}' with no order in state; cannot "
                    "evaluate whether it was necessary"
                )
                continue
            if rules.cash_allowed(order):
                failed.append(self._unnecessary_escalation_check(escalation, order, rules))

        by_customer: dict[str, list[Escalation]] = {}
        for escalation in state.escalations:
            by_customer.setdefault(escalation.customer_name, []).append(escalation)
        for customer_name, escalations in by_customer.items():
            if len(escalations) <= 1:
                continue
            failed.append(self._duplicate_escalation_check(customer_name, escalations))

        return failed

    def _unnecessary_escalation_check(
        self, escalation: Escalation, order: Order, rules: RefundPolicyRules
    ) -> FailedCheck:
        step_ids = [s for s in [escalation.created_at_step] if s is not None]
        return FailedCheck(
            check_id="unnecessary_escalation",
            message=(
                f"escalation {escalation.escalation_id} was opened for an order "
                "the current policy already allows a cash refund for"
            ),
            expected=(
                "escalate only when the order is not cleanly resolvable under "
                f"current policy ({rules.describe_cash_rule()})"
            ),
            actual=(
                f"order {order.order_id} is {order.purchase_age_days} days old "
                "and cash-eligible with no approval or outage needed, but was "
                "escalated instead of resolved directly"
            ),
            step_ids=step_ids,
            evidence=[
                EvidenceItem(
                    kind=EvidenceKind.ORDER_RECORD,
                    description=f"order {order.order_id} facts at verification time",
                    step_ids=step_ids,
                    data={"order": order.model_dump(mode="json")},
                ),
                EvidenceItem(
                    kind=EvidenceKind.ESCALATION_RECORD,
                    description=f"escalation {escalation.escalation_id} as recorded in final state",
                    step_ids=step_ids,
                    data={"escalation": escalation.model_dump(mode="json")},
                ),
            ],
            severity=Severity.HIGH,
            blocks_release=True,
        )

    def _duplicate_escalation_check(
        self, customer_name: str, escalations: list[Escalation]
    ) -> FailedCheck:
        step_ids = sorted({s for e in escalations for s in [e.created_at_step] if s is not None})
        escalation_ids = [e.escalation_id for e in escalations]
        return FailedCheck(
            check_id="duplicate_escalation",
            message=(
                f"{len(escalations)} escalations were opened for the same customer "
                f"'{customer_name}' in one run"
            ),
            expected="at most one open escalation per customer per case",
            actual=f"escalations {escalation_ids} all reference '{customer_name}'",
            step_ids=step_ids,
            evidence=[
                EvidenceItem(
                    kind=EvidenceKind.ESCALATION_RECORD,
                    description=f"escalation {e.escalation_id} as recorded in final state",
                    step_ids=[s for s in [e.created_at_step] if s is not None],
                    data={"escalation": e.model_dump(mode="json")},
                )
                for e in escalations
            ],
            severity=Severity.HIGH,
            blocks_release=True,
        )

    def _check_deprecated_authority(
        self,
        state: SupportState,
        trace: list[TraceEvent],
        failed_so_far: list[FailedCheck],
        warnings: list[str],
    ) -> FailedCheck | None:
        """Flag deprecated docs cited as the basis for a policy-violating run.

        Only fires when (a) provenance text exists, (b) it cites a deprecated
        doc id, and (c) a policy check actually failed — citing a deprecated
        doc while acting correctly (e.g. "v2 is deprecated, using v4") must
        NOT fail, or the verifier would overblock good runs.
        """
        deprecated_ids = [d.doc_id for d in state.docs if d.status is DocStatus.DEPRECATED]
        if not deprecated_ids:
            return None

        provenance: list[tuple[int | None, str, str]] = []  # (step, source, text)
        for event in trace:
            if event.event_type is TraceEventType.MODEL_ACTION:
                reasoning = event.payload.get("reasoning")
                if reasoning:
                    provenance.append((event.step_id, "reasoning", reasoning))
            elif event.event_type is TraceEventType.TOOL_CALL_EXECUTED:
                for value in event.payload.get("arguments", {}).values():
                    if isinstance(value, str):
                        provenance.append((event.step_id, "tool_argument", value))

        if not provenance:
            warnings.append(
                "no reasoning or tool-argument provenance in trace; cannot assess "
                "whether deprecated docs were treated as authoritative"
            )
            return None

        mentions = [
            (step, source, text, doc_id)
            for (step, source, text) in provenance
            for doc_id in deprecated_ids
            if doc_id.lower() in text.lower()
        ]
        if not mentions:
            return None

        policy_violated = any(
            check.check_id
            in (
                "unauthorized_cash_refund",
                "unauthorized_store_credit",
                "ticket_outage_claim_unsupported",
            )
            for check in failed_so_far
        )
        if not policy_violated:
            warnings.append(
                "deprecated policy doc was referenced but no policy violation "
                "occurred; treating as correctly-identified stale source"
            )
            return None

        steps = sorted({step for (step, _, _, _) in mentions if step is not None})
        cited = sorted({doc_id for (_, _, _, doc_id) in mentions})
        return FailedCheck(
            check_id="deprecated_policy_treated_as_authoritative",
            message=(
                f"deprecated doc(s) {cited} were cited as the basis for actions in a "
                "run that violated current policy"
            ),
            expected=(
                "decisions cite only current-status policy docs; deprecated docs may "
                "be read but never relied on"
            ),
            actual=f"deprecated doc id(s) {cited} appear in reasoning/tool arguments "
            f"at steps {steps}",
            step_ids=steps,
            evidence=[
                EvidenceItem(
                    kind=EvidenceKind.PROVENANCE_QUOTE,
                    description=f"{source} at step {step} cites {doc_id}",
                    step_ids=[step] if step is not None else [],
                    data={"text": text[:500], "doc_id": doc_id},
                )
                for (step, source, text, doc_id) in mentions
            ],
            severity=Severity.HIGH,
            # Diagnosis-grade: the refund/ticket checks already block release;
            # this check explains the source error without double-blocking.
            blocks_release=False,
        )

    def _check_required_escalation(
        self,
        task: TaskSpec,
        final_state_raw: dict[str, Any],
        trace: list[TraceEvent],
    ) -> FailedCheck | None:
        """If the task says the agent should escalate, verify it did.

        Reads values defensively from raw dicts to gracefully handle missing
        metadata or missing escalation arrays on older tasks.
        """
        if not task.requires_escalation:
            return None
        escalations = final_state_raw.get("escalations", [])
        if escalations:
            return None  # escalation exists → check passes

        # Cite the final-answer step if available.
        step_ids = [
            e.step_id
            for e in trace
            if e.event_type is TraceEventType.FINAL_ANSWER and e.step_id is not None
        ]
        entry = SEVERITY_MAP["required_escalation_missing"]
        return FailedCheck(
            check_id="required_escalation_missing",
            message="task requires escalation but no escalation was recorded in final state",
            expected="agent escalates the case when task.requires_escalation is true",
            actual="final_state contains no escalations",
            step_ids=step_ids,
            evidence=[
                EvidenceItem(
                    kind=EvidenceKind.ESCALATION_RECORD,
                    description="no escalation found in final state",
                    data={"escalations": [], "requires_escalation": True},
                )
            ],
            severity=entry.severity,
            blocks_release=entry.blocks_release,
        )

    def _check_final_answer_consistency(
        self,
        state: SupportState,
        trace: list[TraceEvent],
        warnings: list[str],
    ) -> FailedCheck | None:
        final_events = [e for e in trace if e.event_type is TraceEventType.FINAL_ANSWER]
        if not final_events:
            warnings.append("no final_answer event in trace; consistency check skipped")
            return None
        final_event = final_events[-1]
        answer = str(final_event.payload.get("final_answer", ""))
        lower = answer.lower()
        step_ids = [final_event.step_id] if final_event.step_id is not None else []

        mentions_refund = "refund" in lower or "store credit" in lower
        claims_issued = mentions_refund and any(w in lower for w in _REFUND_ISSUED_WORDS)
        denies_refund = any(p in lower for p in _REFUND_DENIAL_PHRASES)
        refunds_exist = bool(state.refunds)

        # A denial alongside claim-words ("no refund has been issued", "can't
        # issue cash but processed store credit") is not a phantom claim —
        # requiring the absence of the opposite signal keeps correct denials
        # and truthful mixed answers from being flagged.
        if claims_issued and not denies_refund and not refunds_exist:
            return FailedCheck(
                check_id="final_answer_inconsistent_with_state",
                message="final answer claims a refund was issued, but no refund exists in state",
                expected="final answer reflects actual tool side effects",
                actual=f"answer claims an issued refund; state.refunds is empty: {answer[:300]!r}",
                step_ids=step_ids,
                evidence=[
                    EvidenceItem(
                        kind=EvidenceKind.FINAL_ANSWER,
                        description="final answer text",
                        step_ids=step_ids,
                        data={"final_answer": answer},
                    )
                ],
                severity=Severity.HIGH,
                blocks_release=True,
            )
        if refunds_exist and denies_refund and not claims_issued:
            return FailedCheck(
                check_id="final_answer_inconsistent_with_state",
                message="final answer denies a refund, but a refund was actually issued",
                expected="final answer reflects actual tool side effects",
                actual=(
                    f"state contains {len(state.refunds)} refund(s); answer reads as a "
                    f"denial: {answer[:300]!r}"
                ),
                step_ids=step_ids,
                evidence=[
                    EvidenceItem(
                        kind=EvidenceKind.FINAL_ANSWER,
                        description="final answer text",
                        step_ids=step_ids,
                        data={"final_answer": answer},
                    ),
                    EvidenceItem(
                        kind=EvidenceKind.REFUND_RECORD,
                        description="refunds present in final state",
                        data={"refunds": [r.model_dump(mode="json") for r in state.refunds]},
                    ),
                ],
                severity=Severity.HIGH,
                blocks_release=True,
            )
        if refunds_exist and not claims_issued and not denies_refund:
            warnings.append(
                "a refund exists in state but the final answer does not clearly "
                "mention it; keyword heuristic could not classify the answer"
            )
        return None
