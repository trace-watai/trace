"""Typed in-memory state for the support/refund environment.

This is the mutable "world" of the first vertical slice: orders, refunds,
tickets, and the document corpus. The environment owns one
:class:`SupportState` per run, mutates it through tool handlers, and
snapshots it at run start and end so verifiers judge *actual* side effects,
never the agent's claims about them.

Design notes
    - ``purchase_age_days`` is stored directly instead of purchase dates so
      fixtures stay deterministic (no clock reads anywhere in a run).
    - Side-effect records carry ``*_at_step`` provenance so verifier evidence
      can point at exact trace steps.
    - ``Doc.status`` is load-bearing: the refund failure scenario hinges on
      the agent ignoring ``deprecated`` vs ``current`` metadata.

What does NOT belong here
    Policy *judgment* (that is verifier territory) and retrieval scoring
    (``retrieval.py``). State is dumb data plus lookups.

# TODO(Evan Yang/environment): when a second workflow (non-refund) lands,
# extract the generic parts (docs, snapshotting) from the support-specific
# parts instead of growing this file sideways.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from trace_harness.tasks.schemas import TaskSpec

STATE_SCHEMA_VERSION = "0.1.0"


class DocStatus(StrEnum):
    """Lifecycle status of a knowledge-base document.

    ``current`` is authoritative; ``deprecated`` exists but must not drive
    decisions; ``resolved`` marks closed incident notices that read as
    relevant but no longer apply.
    """

    CURRENT = "current"
    DEPRECATED = "deprecated"
    RESOLVED = "resolved"


def _next_seq(prefix: str, existing_ids: Iterable[str]) -> int:
    """Smallest sequence number that won't collide with ``<prefix>-NNNN`` ids."""
    pattern = re.compile(rf"{prefix}-(\d+)$")
    taken = [int(m.group(1)) for i in existing_ids if (m := pattern.match(i))]
    return max(taken, default=0) + 1


class Doc(BaseModel):
    """A knowledge-base document available to retrieval.

    ``metadata`` may carry machine-readable structure; the refund policy doc
    uses ``metadata["rules"]`` to hold the structured policy rules the
    verifier evaluates (kept in sync with the human-readable ``content`` —
    see fixtures/README.md).
    """

    model_config = ConfigDict(extra="forbid")

    doc_id: str
    title: str
    status: DocStatus
    content: str
    source: str = "fixture"
    # ISO date string from the fixture, never auto-generated.
    last_updated: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RefundType(StrEnum):
    CASH = "cash"
    STORE_CREDIT = "store_credit"


class Order(BaseModel):
    """One customer order, with the facts refund policy decisions hinge on."""

    model_config = ConfigDict(extra="forbid")

    order_id: str
    customer_name: str
    plan: str
    # ge=0 bounds: a sign typo in a fixture must fail loudly, not silently
    # authorize a refund (-47 <= 30) or corrupt blast-radius math.
    amount_usd: float = Field(ge=0)
    purchase_age_days: int = Field(ge=0)
    documented_outage_near_purchase: bool = False
    manager_approval_granted: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class Refund(BaseModel):
    """A refund side effect created by the ``issue_refund`` tool."""

    model_config = ConfigDict(extra="forbid")

    refund_id: str
    order_id: str
    customer_name: str
    refund_type: RefundType
    amount_usd: float = Field(ge=0)
    reason: str
    issued_at_step: int | None = None


class Ticket(BaseModel):
    """A support-ticket side effect created by the ``create_ticket`` tool."""

    model_config = ConfigDict(extra="forbid")

    ticket_id: str
    customer_name: str
    title: str
    notes: str
    created_at_step: int | None = None


class Escalation(BaseModel):
    """An escalation side effect created by the ``escalate_case`` tool."""

    model_config = ConfigDict(extra="forbid")

    escalation_id: str
    customer_name: str
    reason: str
    created_at_step: int | None = None


class SupportState(BaseModel):
    """The complete mutable world of the support/refund environment.

    ``extra="forbid"`` across this module: a typo'd key in a task's
    ``initial_state`` (e.g. ``"ordes"``) must fail at load time, not produce
    an empty world the agent then "fails" in for unrelated reasons.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: str = STATE_SCHEMA_VERSION
    orders: list[Order] = Field(default_factory=list)
    refunds: list[Refund] = Field(default_factory=list)
    tickets: list[Ticket] = Field(default_factory=list)
    escalations: list[Escalation] = Field(default_factory=list)
    docs: list[Doc] = Field(default_factory=list)
    # Counters keep generated IDs deterministic (REF-0001, TICK-0001, ESC-0001, ...).
    next_refund_seq: int = 1
    next_ticket_seq: int = 1
    next_escalation_seq: int = 1
    # Optional fixture-pinned result order for search_docs. When set, docs
    # whose doc_id appears in this list are returned first (in list order)
    # regardless of keyword score. Remaining matches follow by score desc.
    doc_ranking_override: list[str] | None = Field(default=None)

    @classmethod
    def from_task(cls, task: TaskSpec, docs: list[Doc] | None = None) -> SupportState:
        """Build initial state from a task's ``initial_state`` plus loaded docs.

        Docs may come from a docs fixture (``docs`` argument, loaded by
        ``tasks.loader.load_docs_for_task``) and/or inline under
        ``initial_state["docs"]``; fixture docs come first, inline docs are
        appended.
        """
        payload = dict(task.initial_state)
        inline_docs = payload.pop("docs", [])
        state = cls.model_validate(payload)
        state.docs = list(docs or []) + [Doc.model_validate(d) for d in inline_docs]
        # Seed ID counters past any pre-existing records so a scenario that
        # starts with REF-0001 in history can't mint a colliding REF-0001.
        if "next_refund_seq" not in payload:
            state.next_refund_seq = _next_seq("REF", (r.refund_id for r in state.refunds))
        if "next_ticket_seq" not in payload:
            state.next_ticket_seq = _next_seq("TICK", (t.ticket_id for t in state.tickets))
        if "next_escalation_seq" not in payload:
            state.next_escalation_seq = _next_seq(
                "ESC", (e.escalation_id for e in state.escalations)
            )
        return state

    def find_order(self, customer_name: str) -> Order | None:
        """Case-insensitive exact-name lookup; first match wins.

        MVP assumption: one order per customer per task. Multi-order
        customers need an order_id-based tool signature first.
        """
        wanted = customer_name.strip().lower()
        for order in self.orders:
            if order.customer_name.strip().lower() == wanted:
                return order
        return None

    def snapshot(self) -> dict[str, Any]:
        """A JSON-safe deep copy of the entire state, for trace/artifact use."""
        return self.model_dump(mode="json")
