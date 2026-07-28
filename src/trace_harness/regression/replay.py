"""Replay uses the pinned state from the regression artifact, not the current fixture,
to ensure regression checks are stable even if fixtures change. State drift is detected
and reported as informational only—the artifact remains authoritative."""

from __future__ import annotations

from typing import Any

from trace_harness.models.fixture import FixtureScript
from trace_harness.regression.schemas import RegressionArtifact

# Identity field per state collection, for diffing record-by-record instead
# of reporting a whole list as "changed".
_RECORD_ID_FIELDS: dict[str, str] = {
    "orders": "order_id",
    "docs": "doc_id",
    "refunds": "refund_id",
    "tickets": "ticket_id",
    "escalations": "escalation_id",
}


def pinned_initial_state(artifact: RegressionArtifact) -> dict[str, Any]:
    """The exact ``SupportState`` payload this artifact recorded.

    ``pinned_docs`` is a projection of ``initial_state["docs"]`` for readers
    that only care about the corpus, so normally there is nothing to
    reconcile. It is used as a fallback for artifacts written without inline
    docs, so replaying one doesn't silently produce a docless world.
    """
    state = dict(artifact.initial_state)
    if not state.get("docs") and artifact.pinned_docs:
        state["docs"] = [dict(doc) for doc in artifact.pinned_docs]
    return state


def pinned_script(artifact: RegressionArtifact, task_id: str) -> FixtureScript | None:
    """A replayable script built from the artifact's recorded agent actions.

    ``None`` for artifacts written before schema 0.2.0, which pinned no
    actions — those still replay from the script the task fixture names.
    """
    if not artifact.pinned_agent_actions:
        return None
    return FixtureScript.model_validate(
        {
            "script_id": f"pinned_{artifact.test_name}",
            "task_id": task_id,
            "description": (
                f"Agent actions recorded in run {artifact.source_run_id} and pinned "
                "into this regression artifact."
            ),
            "actions": artifact.pinned_agent_actions,
        }
    )


def describe_action_drift(pinned: list[dict[str, Any]], live: list[dict[str, Any]]) -> list[str]:
    """Differences between the pinned agent moves and the fixture script's moves.

    Same contract as :func:`describe_state_drift`: informational only. Compares
    the fields that decide what the agent *does* — ``reasoning`` is authoring
    commentary and is deliberately ignored, so rewording a script's narration
    is not reported as drift.
    """
    notes: list[str] = []
    if len(pinned) != len(live):
        notes.append(f"script length: {len(pinned)} pinned action(s) -> {len(live)} in the fixture")

    def _material(action: dict[str, Any]) -> dict[str, Any]:
        return {
            "kind": action.get("kind"),
            "tool_call": action.get("tool_call"),
            "final_answer": action.get("final_answer"),
        }

    def _short(value: Any) -> str:
        """Final answers are paragraphs; keep a drift line readable."""
        rendered = repr(value)
        return rendered if len(rendered) <= 80 else rendered[:77] + "..."

    for index, (pinned_action, live_action) in enumerate(zip(pinned, live, strict=False), start=1):
        before, after = _material(pinned_action), _material(live_action)
        if before == after:
            continue
        changed = sorted(field for field in before if before[field] != after[field])
        detail = ", ".join(
            f"{field}: {_short(before[field])} -> {_short(after[field])}" for field in changed
        )
        notes.append(f"action {index} changed ({detail})")
    return notes


def _diff_records(collection: str, pinned: list[Any], live: list[Any]) -> list[str]:
    id_field = _RECORD_ID_FIELDS[collection]
    pinned_by_id = {str(r.get(id_field)): r for r in pinned if isinstance(r, dict)}
    live_by_id = {str(r.get(id_field)): r for r in live if isinstance(r, dict)}

    notes = []
    if removed := sorted(set(pinned_by_id) - set(live_by_id)):
        notes.append(f"{collection}: pinned {removed} no longer in the fixture")
    if added := sorted(set(live_by_id) - set(pinned_by_id)):
        notes.append(f"{collection}: fixture has new {added} not in the pinned run")
    for record_id in sorted(set(pinned_by_id) & set(live_by_id)):
        pinned_record, live_record = pinned_by_id[record_id], live_by_id[record_id]
        fields = sorted(
            field
            for field in set(pinned_record) | set(live_record)
            if pinned_record.get(field) != live_record.get(field)
        )
        if fields:
            changed = ", ".join(
                f"{field}: {pinned_record.get(field)!r} -> {live_record.get(field)!r}"
                for field in fields
            )
            notes.append(f"{collection}[{record_id}] changed ({changed})")
    return notes


def describe_state_drift(pinned: dict[str, Any], live: dict[str, Any]) -> list[str]:
    """Human-readable differences between a pinned world and the current fixture's.

    Empty list means the fixture still describes the world this regression
    was pinned from. A non-empty list does not invalidate the replay — the
    pinned world is what ran — it means the fixture has moved on and someone
    should decide which one is right.
    """
    notes: list[str] = []
    for collection in _RECORD_ID_FIELDS:
        pinned_value = pinned.get(collection) or []
        live_value = live.get(collection) or []
        if isinstance(pinned_value, list) and isinstance(live_value, list):
            notes.extend(_diff_records(collection, pinned_value, live_value))

    for key in sorted((set(pinned) | set(live)) - set(_RECORD_ID_FIELDS)):
        if pinned.get(key) != live.get(key):
            notes.append(f"{key}: {pinned.get(key)!r} -> {live.get(key)!r}")
    return notes
