"""Mock support/refund environment: state models, tool implementations, and inspection hooks.

All tools follow the runner's dispatch signature ``(args: dict, state: dict) -> dict``
and never raise — errors are returned inside the result dict with ``ok: False``.
State is a plain dict so the runner can deepcopy it without knowing about Pydantic.
Pydantic is used at the boundaries: fixture construction and post-run inspection.
"""

from __future__ import annotations

import copy
from typing import Callable

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# State models
# ---------------------------------------------------------------------------


class OrderRecord(BaseModel):
    """A single customer order tracked by the environment."""

    amount: float
    refunded: bool = False


class RefundRecord(BaseModel):
    """Immutable record written when a refund is successfully issued."""

    order_id: str
    amount: float


class TicketRecord(BaseModel):
    """Support ticket opened against an order."""

    ticket_id: str
    order_id: str
    reason: str
    status: str = "open"  # "open" | "closed"


class EnvironmentState(BaseModel):
    """Complete snapshot of the in-memory world the agent acts on.

    All three collections default to empty so ``model_validate`` works on
    initial-state dicts that don't yet include refunds or tickets keys.
    """

    orders: dict[str, OrderRecord] = Field(default_factory=dict)
    refunds: list[RefundRecord] = Field(default_factory=list)
    tickets: dict[str, TicketRecord] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Tool arg / result schemas
# ---------------------------------------------------------------------------


class GetOrderArgs(BaseModel):
    order_id: str


class GetOrderResult(BaseModel):
    ok: bool
    order_id: str | None = None
    amount: float | None = None
    refunded: bool | None = None
    error: str | None = None


class IssueRefundArgs(BaseModel):
    order_id: str
    amount: float


class IssueRefundResult(BaseModel):
    ok: bool
    refunded: float | None = None
    error: str | None = None


class CreateTicketArgs(BaseModel):
    order_id: str
    reason: str


class CreateTicketResult(BaseModel):
    ok: bool
    ticket_id: str | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def get_order(args: dict, state: dict) -> dict:
    """Read-only lookup. Returns order fields or an error if the order is missing."""
    try:
        parsed = GetOrderArgs.model_validate(args)
    except Exception as exc:
        return {"ok": False, "error": f"invalid args: {exc}"}

    order = state.get("orders", {}).get(parsed.order_id)
    if order is None:
        return GetOrderResult(ok=False, error=f"unknown order: {parsed.order_id!r}").model_dump(
            exclude_none=True
        )

    return GetOrderResult(
        ok=True,
        order_id=parsed.order_id,
        amount=order["amount"],
        refunded=order["refunded"],
    ).model_dump(exclude_none=True)


def issue_refund(args: dict, state: dict) -> dict:
    """Mark an order as refunded and append a RefundRecord to state["refunds"].

    Rejects duplicate refunds and unknown orders without mutating state.
    """
    try:
        parsed = IssueRefundArgs.model_validate(args)
    except Exception as exc:
        return {"ok": False, "error": f"invalid args: {exc}"}

    order = state.get("orders", {}).get(parsed.order_id)
    if order is None:
        return IssueRefundResult(
            ok=False, error=f"unknown order: {parsed.order_id!r}"
        ).model_dump(exclude_none=True)

    if order.get("refunded"):
        return IssueRefundResult(ok=False, error="order already refunded").model_dump(
            exclude_none=True
        )

    order["refunded"] = True
    state.setdefault("refunds", []).append(
        {"order_id": parsed.order_id, "amount": parsed.amount}
    )
    return IssueRefundResult(ok=True, refunded=parsed.amount).model_dump(exclude_none=True)


def create_ticket(args: dict, state: dict) -> dict:
    """Open a support ticket against an existing order.

    Ticket IDs are deterministic (ticket_001, ticket_002, …) so fixture runs
    produce identical traces across executions.
    """
    try:
        parsed = CreateTicketArgs.model_validate(args)
    except Exception as exc:
        return {"ok": False, "error": f"invalid args: {exc}"}

    if parsed.order_id not in state.get("orders", {}):
        return CreateTicketResult(
            ok=False, error=f"unknown order: {parsed.order_id!r}"
        ).model_dump(exclude_none=True)

    existing = state.setdefault("tickets", {})
    ticket_id = f"ticket_{len(existing) + 1:03d}"
    existing[ticket_id] = {
        "ticket_id": ticket_id,
        "order_id": parsed.order_id,
        "reason": parsed.reason,
        "status": "open",
    }
    return CreateTicketResult(ok=True, ticket_id=ticket_id).model_dump(exclude_none=True)


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

TOOLS: dict[str, Callable[[dict, dict], dict]] = {
    "get_order": get_order,
    "issue_refund": issue_refund,
    "create_ticket": create_ticket,
}


# ---------------------------------------------------------------------------
# SupportEnvironment
# ---------------------------------------------------------------------------


class SupportEnvironment:
    """Factory and inspector for the support/refund in-memory world.

    Holds the authoritative initial state and provides helpers for resetting,
    snapshotting, and converting raw state dicts back to typed models. The
    runner's own ``copy.deepcopy(task.initial_state)`` is what actually isolates
    each run; ``reset()`` gives the same guarantee when calling tools directly.

    Typical test usage::

        env = SupportEnvironment(task.initial_state)
        state = env.reset()
        result = issue_refund({"order_id": "o1", "amount": 10.0}, state)
        typed = env.inspect(state)
        assert typed.orders["o1"].refunded
    """

    def __init__(self, initial_state: dict) -> None:
        self._initial_state = initial_state

    @classmethod
    def from_task(cls, task: object) -> "SupportEnvironment":
        """Convenience constructor that reads ``task.initial_state`` directly."""
        return cls(task.initial_state)  # type: ignore[attr-defined]

    def reset(self) -> dict:
        """Return a fresh deepcopy of the initial state for a new run."""
        return copy.deepcopy(self._initial_state)

    def initial_snapshot(self) -> dict:
        """Deepcopy of the initial state — use for before/after comparison."""
        return copy.deepcopy(self._initial_state)

    def final_snapshot(self, state: dict) -> dict:
        """Deepcopy of ``state`` as it stands after a run completes."""
        return copy.deepcopy(state)

    def inspect(self, state_dict: dict) -> EnvironmentState:
        """Parse a raw state dict into a typed ``EnvironmentState`` for assertions."""
        return EnvironmentState.model_validate(state_dict)
