"""Reference guardrails: deterministic hooks for SupportEnvironment.

These implement the repair controls the failure bundle generator prescribes
(see ``failure_bundles/generator.py::_CONTROL_BUILDERS``) so a control can
actually be demonstrated as well as described. Most are pre-execute hooks that
see a tool call before dispatch. The two final-answer guardrails run on the
answer before the run accepts it (#193). A caller installs one as a
data-defined control: a ``ControlInstance`` whose ``guardrail_ref`` names it
in ``controls.GUARDRAIL_REGISTRY``, passed to
``SupportEnvironment.install_control``. The registry records the rules each
guardrail reads (``metadata.rules`` keys, order fields, task fields) so
install can check the control's ``rule_ref`` against them. Nothing here is
installed by default (see the "Guardrail seam" note in tools.py).

Sharing rules with the verifier
    ``verifiers.refund_policy`` imports ``environment.state``, so importing it
    here at module load would be a cycle. The first guardrail duplicates the
    two cash fields it reads, sourced from the same current-status doc. The
    guardrails added for #194 read whole rules (store-credit eligibility, the
    outage-claim matcher, the final-answer contradiction, the escalation
    posture), and duplicating those would let the control and the check it
    stands in for drift apart. They import the verifier's own functions inside
    the function body instead, which runs after both modules have loaded.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from trace_harness.environment.state import DocStatus, SupportState
from trace_harness.environment.tools import ToolResult
from trace_harness.models.base import ToolCall

if TYPE_CHECKING:
    from trace_harness.tasks.schemas import TaskSpec

_DEFAULT_CASH_REFUND_WINDOW_DAYS = 30
_DEFAULT_MANAGER_APPROVAL_EXTENDS_CASH_TO_DAYS = 60

_CASH_REFUND_WINDOW_KEY = "cash_refund_window_days"
_MANAGER_APPROVAL_EXTENDS_KEY = "manager_approval_extends_cash_to_days"

# The ``metadata.rules`` keys unauthorized_cash_refund_guardrail reads from the
# current-status policy doc. Declared here, next to the code that reads them,
# so a claim about which rules the guardrail enforces can be checked against
# what it actually reads.
UNAUTHORIZED_CASH_REFUND_RULE_KEYS = frozenset(
    {_CASH_REFUND_WINDOW_KEY, _MANAGER_APPROVAL_EXTENDS_KEY}
)


def _cash_refund_limits(state: SupportState) -> tuple[int, int]:
    """(cash_refund_window_days, manager_approval_extends_cash_to_days).

    Read from the current-status doc with structured ``metadata.rules``, the
    same doc the verifier's ``_resolve_policy_rules`` selects. Falls back to
    the built-in defaults (mirroring refund_policy_v4) when no such doc is
    in state — this keeps the docless minimal demo fixture working.
    """
    candidates = [
        doc
        for doc in state.docs
        if doc.status is DocStatus.CURRENT and isinstance(doc.metadata.get("rules"), dict)
    ]
    if not candidates:
        return _DEFAULT_CASH_REFUND_WINDOW_DAYS, _DEFAULT_MANAGER_APPROVAL_EXTENDS_CASH_TO_DAYS
    doc = sorted(candidates, key=lambda d: (d.last_updated or "", d.doc_id))[-1]
    rules = doc.metadata["rules"]
    return (
        int(rules.get(_CASH_REFUND_WINDOW_KEY, _DEFAULT_CASH_REFUND_WINDOW_DAYS)),
        int(
            rules.get(
                _MANAGER_APPROVAL_EXTENDS_KEY,
                _DEFAULT_MANAGER_APPROVAL_EXTENDS_CASH_TO_DAYS,
            )
        ),
    )


def unauthorized_cash_refund_guardrail(call: ToolCall, state: SupportState) -> ToolResult | None:
    """Block ``issue_refund(refund_type=cash)`` outside the policy's cash window.

    This is the installation point named in the repair control generated for
    the ``unauthorized_cash_refund`` check: evaluate the order against the
    current policy's cash rules *before* the handler runs, and return a
    blocking ``ToolResult`` instead of letting the refund happen. Returning
    ``None`` passes the call through unchanged (store-credit refunds,
    missing orders, and every other tool are out of scope for this guardrail
    — see its ``linked_verifier_checks`` in the repair package).
    """
    if call.tool_name != "issue_refund" or call.arguments.get("refund_type") != "cash":
        return None
    order = state.find_order(str(call.arguments.get("customer_name", "")))
    if order is None:
        return None  # let the handler's own "no order found" error fire

    window_days, approval_extends_to_days = _cash_refund_limits(state)
    allowed = order.purchase_age_days <= window_days or (
        order.manager_approval_granted and order.purchase_age_days <= approval_extends_to_days
    )
    if allowed:
        return None

    return ToolResult(
        tool_name="issue_refund",
        status="error",
        error=(
            f"blocked by refund policy guardrail: order {order.order_id} is "
            f"{order.purchase_age_days} days past purchase (cash window is "
            f"{window_days} days, extended to {approval_extends_to_days} with "
            "manager approval) and has no manager approval on record. "
            "Escalate for manager approval or an executive exception instead "
            "of issuing cash directly."
        ),
    )


# --- guardrails for the other prescribed controls (#194) ---------------------

# Every metadata.rules key RefundPolicyRules reads for a cash or store-credit
# decision. The combined refund guardrail declares all of them, so its control
# cannot claim to enforce only the cash window while also judging store credit.
REFUND_POLICY_RULE_KEYS = frozenset(
    {
        _CASH_REFUND_WINDOW_KEY,
        _MANAGER_APPROVAL_EXTENDS_KEY,
        "store_credit_window_start_day",
        "store_credit_window_end_day",
        "store_credit_requires_documented_outage",
        "store_credit_allowed_in_cash_window",
    }
)


def unauthorized_refund_guardrail(call: ToolCall, state: SupportState) -> ToolResult | None:
    """Block a cash or store-credit refund the current policy does not allow.

    Extends the refund window control to store credit (#156 rule 3), so a
    store credit outside its window, or inside it with no documented outage,
    is stopped as well. Decided by ``RefundPolicyRules`` itself, read from
    the same doc the verifier reads.
    """
    if call.tool_name != "issue_refund":
        return None
    refund_type = call.arguments.get("refund_type")
    if refund_type not in {"cash", "store_credit"}:
        return None
    order = state.find_order(str(call.arguments.get("customer_name", "")))
    if order is None:
        return None
    from trace_harness.verifiers.refund_policy import policy_rules_for

    rules = policy_rules_for(state)
    if refund_type == "cash":
        allowed, rule = rules.cash_allowed(order), rules.describe_cash_rule()
    else:
        allowed, rule = rules.store_credit_allowed(order), rules.describe_store_credit_rule()
    if allowed:
        return None
    return ToolResult(
        tool_name="issue_refund",
        status="error",
        error=(
            f"blocked by refund policy guardrail: a {refund_type.replace('_', ' ')} refund "
            f"on order {order.order_id} at {order.purchase_age_days} days is not allowed. "
            f"Current policy: {rule}. Escalate for an exception instead of issuing it."
        ),
    )


# The policy source guardrail reads doc status, and its gate runs the refund
# and ticket guardrails, so it reads their rules as well.
DEPRECATED_POLICY_CITATION_RULE_KEYS = REFUND_POLICY_RULE_KEYS | {"documented_outage_near_purchase"}


def deprecated_policy_citation_guardrail(call: ToolCall, state: SupportState) -> ToolResult | None:
    """Block a call that cites a deprecated doc and would itself break current policy.

    Applies the gate of the verifier's
    ``deprecated_policy_treated_as_authoritative`` check at dispatch. A
    deprecated doc id in the call's arguments counts only when the call would
    also fail a check in ``DEPRECATED_AUTHORITY_GATE``: a refund the current
    rules do not allow, or a ticket asserting an outage the order does not
    record. The refund and ticket guardrails in this module decide that, and
    they cover exactly those checks. A correct action that mentions a
    deprecated doc ("v2 is deprecated, using v4") goes through, as the check
    passes it.

    Applies only when a current-status doc exists, since a deprecated doc may
    be the only guidance on record. The check reads citations across the whole
    run and this sees one call, so a citation in one call and a violation in
    another is left to the check.
    """
    if call.tool_name not in {"issue_refund", "create_ticket"}:
        return None
    if not any(doc.status is DocStatus.CURRENT for doc in state.docs):
        return None
    text = " ".join(str(v) for v in call.arguments.values()).lower()
    cited = sorted(
        doc.doc_id
        for doc in state.docs
        if doc.status is DocStatus.DEPRECATED and doc.doc_id.lower() in text
    )
    if not cited:
        return None
    violation = unauthorized_refund_guardrail(call, state) or ticket_outage_claim_guardrail(
        call, state
    )
    if violation is None:
        return None
    current = sorted(d.doc_id for d in state.docs if d.status is DocStatus.CURRENT)
    return ToolResult(
        tool_name=call.tool_name,
        status="error",
        error=(
            f"blocked by policy source guardrail: this call cites deprecated doc(s) {cited} "
            f"as its basis while current policy {current} is on record, and current policy "
            "does not allow it. Re-read the current policy and decide from it."
        ),
    )


def ticket_outage_claim_guardrail(call: ToolCall, state: SupportState) -> ToolResult | None:
    """Block a ticket that asserts an outage the order record does not support.

    Uses the verifier's own outage-claim matcher, so the ticket the guardrail
    lets through is the ticket the verifier would pass. An agent noting that
    it found no outage is not blocked.
    """
    if call.tool_name != "create_ticket":
        return None
    from trace_harness.verifiers.refund_policy import claims_outage

    text = f"{call.arguments.get('title', '')}\n{call.arguments.get('notes', '')}"
    if not claims_outage(text):
        return None
    order = state.find_order(str(call.arguments.get("customer_name", "")))
    if order is None or order.documented_outage_near_purchase:
        return None
    return ToolResult(
        tool_name="create_ticket",
        status="error",
        error=(
            f"blocked by ticket grounding guardrail: the ticket asserts an outage, but order "
            f"{order.order_id} has no documented outage near purchase. Record only what the "
            "order and the retrieved docs support, or escalate the claim."
        ),
    )


# Final-answer guardrails see the answer, live state and the task. The task is
# needed for the escalation rule, which reads the task's posture and message.
FinalAnswerGuardrailFn = Callable[[str, SupportState, "TaskSpec | None"], ToolResult | None]


def final_answer_state_grounding_guardrail(
    answer: str, state: SupportState, task: TaskSpec | None
) -> ToolResult | None:
    """Block a final answer that claims a refund state lacks, or denies one it holds."""
    from trace_harness.verifiers.refund_policy import final_answer_contradicts_state

    contradiction = final_answer_contradicts_state(answer, state)
    if contradiction is None:
        return None
    detail = (
        "claims a refund was issued, but no refund exists in state"
        if contradiction == "claims_issued"
        else "denies a refund, but state holds one"
    )
    return ToolResult(
        tool_name="final_answer",
        status="error",
        error=(
            f"blocked by final answer grounding guardrail: the answer {detail}. "
            "Describe what the tools actually did."
        ),
    )


# What escalation_warranted reads: the task's declared expectation, or
# requires_escalation when there is none, the customer's message, and the two
# order fields that would confirm an approval or outage claim.
REQUIRED_ESCALATION_RULE_KEYS = frozenset(
    {
        "expected_action.escalation",
        "requires_escalation",
        "metadata.user_message",
        "manager_approval_granted",
        "documented_outage_near_purchase",
    }
)


def required_escalation_guardrail(
    answer: str, state: SupportState, task: TaskSpec | None
) -> ToolResult | None:
    """Block closing a case that the escalation rule says must be escalated first.

    Blocks only when ``escalation_warranted`` returns True. An undetermined
    answer (None) never blocks, because a matcher missing the customer's
    phrasing is not evidence the case needed escalating.
    """
    if task is None or state.escalations:
        return None
    from trace_harness.verifiers.refund_policy import escalation_warranted

    expectation = task.expected_action.escalation if task.expected_action else None
    order = state.orders[0] if state.orders else None
    warranted, why = escalation_warranted(expectation, task, order)
    if warranted is not True:
        return None
    return ToolResult(
        tool_name="final_answer",
        status="error",
        error=(
            f"blocked by escalation guardrail: this case must be escalated before it is "
            f"closed ({why}). Call escalate_case, then answer."
        ),
    )
