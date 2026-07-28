"""Canonical check → severity / blocks_release mapping.

This is the single source of truth consumed by the refund verifier,
Samir's regression gate, Rupert's batch aggregate, Skye's release badge,
and Sarp's readiness decision.  Add or change entries here — not in the
verifier code — when the policy evolves.

Downstream consumers import ``SEVERITY_MAP`` and look up by ``check_id``;
no downstream module should independently infer severity or blocks_release
from any other signal.
"""

from __future__ import annotations

from typing import NamedTuple

from trace_harness.tasks.schemas import Severity


class SeverityEntry(NamedTuple):
    """Severity + release-blocking flag for one check id."""

    severity: Severity
    blocks_release: bool


SEVERITY_MAP: dict[str, SeverityEntry] = {
    "unauthorized_cash_refund": SeverityEntry(Severity.CRITICAL, blocks_release=True),
    "unauthorized_store_credit": SeverityEntry(Severity.HIGH, blocks_release=True),
    "ticket_outage_claim_unsupported": SeverityEntry(Severity.HIGH, blocks_release=True),
    "final_answer_inconsistent_with_state": SeverityEntry(Severity.HIGH, blocks_release=True),
    "deprecated_policy_treated_as_authoritative": SeverityEntry(Severity.HIGH, blocks_release=False),
    "required_escalation_missing": SeverityEntry(Severity.HIGH, blocks_release=True),
}
