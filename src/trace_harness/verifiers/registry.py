"""Verifier registry: verifier_id -> implementation lookup.

Tasks reference verifiers by id (``TaskSpec.verifier_ids``); this is the one
place ids resolve to classes. Keep registration explicit — a decorator-based
auto-registry can come later if the list grows past a screenful.

# TODO(Karan/verifier): add the next domain verifier here (and only here);
# ids are a public contract used by repair packages and regression artifacts.
"""

from __future__ import annotations

from trace_harness.verifiers.base import Verifier
from trace_harness.verifiers.refund_policy import RefundPolicyVerifier

VERIFIER_REGISTRY: dict[str, type[Verifier]] = {
    RefundPolicyVerifier.verifier_id: RefundPolicyVerifier,
}


def available_verifier_ids() -> list[str]:
    return sorted(VERIFIER_REGISTRY)


def get_verifier(verifier_id: str) -> Verifier:
    cls = VERIFIER_REGISTRY.get(verifier_id)
    if cls is None:
        raise KeyError(
            f"unknown verifier_id '{verifier_id}'; available: {available_verifier_ids()}"
        )
    return cls()
