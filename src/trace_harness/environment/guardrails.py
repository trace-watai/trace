"""Reference guardrails: deterministic pre-execute hooks for SupportEnvironment.

These implement the repair controls the failure bundle generator prescribes
(see ``failure_bundles/generator.py::_control_refund_guardrail``) so the
control can actually be demonstrated, not just described. A caller wires one
in with ``SupportEnvironment.register_pre_execute_hook`` — nothing here is
registered by default (see the "Guardrail seam" note in tools.py).

Why this doesn't import trace_harness.verifiers
    ``verifiers.refund_policy`` already imports ``environment.state``. If a
    guardrail here imported back from ``verifiers``, that would be a cycle.
    The handful of policy fields read below are duplicated on purpose,
    sourced from the same place the verifier reads them (the current-status
    doc's ``metadata.rules``) so a policy doc change updates both sides. If
    the two ever need more than these two fields in common, that's the
    signal to extract a shared, dependency-free rules module instead of
    duplicating further.
"""

from __future__ import annotations

from trace_harness.environment.state import DocStatus, SupportState
from trace_harness.environment.tools import ToolResult
from trace_harness.models.base import ToolCall

_DEFAULT_CASH_REFUND_WINDOW_DAYS = 30
_DEFAULT_MANAGER_APPROVAL_EXTENDS_CASH_TO_DAYS = 60


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
        int(rules.get("cash_refund_window_days", _DEFAULT_CASH_REFUND_WINDOW_DAYS)),
        int(
            rules.get(
                "manager_approval_extends_cash_to_days",
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
