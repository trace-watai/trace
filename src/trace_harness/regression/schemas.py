"""Schemas for regression artifacts.

A regression artifact pins everything needed to re-test a failure class
later: the task, the initial state, the exact docs the agent saw, the
checks that must hold, and a replay command. It also carries *positive
sibling tests* — nearby scenarios that must keep PASSING — so the fix for a
failure cannot quietly overblock legitimate behavior.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from trace_harness.tasks.schemas import Severity

# 0.2.0: added pinned_agent_actions so the agent's moves are pinned alongside
# the world they ran in (previously only state and docs were).
# 0.3.0: replay trust label and the facts used to predict it.
REGRESSION_SCHEMA_VERSION = "0.3.0"
ReplayMode = Literal["static_ok", "live_required", "unlabeled"]
# Who produced a replay label: the materializer's fixed rule, or a live
# measurement (#159).
ReplayModePredictor = Literal["heuristic_v1", "measured"]


class ReplayModeBasis(BaseModel):
    control_ids: list[str] = Field(default_factory=list)
    control_step: int | None = None
    first_irreversible_action_step: int | None = None
    steps_remaining_after_control: int | None = None
    gated_tool: str | None = None
    checks_reachable_via_gated_tool: list[str] = Field(default_factory=list)
    checks_covered_by_control: list[str] = Field(default_factory=list)
    other_irreversible_tools: list[str] = Field(default_factory=list)
    rule_kind: Literal["prohibition", "requirement"] | None = None
    predicted_by: ReplayModePredictor = "heuristic_v1"
    agreement_rate: float | None = Field(default=None, ge=0, le=1)
    source_experiment_id: str | None = None


def classify_replay_mode(basis: ReplayModeBasis) -> ReplayMode:
    """All four conditions need affirmative evidence; missing facts fail closed.

    Lives beside the basis it reads so the control library and the metrics
    can re-check a stored label without importing the materializer.
    """
    if (
        basis.control_ids
        and basis.control_step is not None
        and basis.control_step == basis.first_irreversible_action_step
        and basis.rule_kind == "prohibition"
        and basis.gated_tool
        and basis.checks_reachable_via_gated_tool
        and set(basis.checks_reachable_via_gated_tool) <= set(basis.checks_covered_by_control)
        and not basis.other_irreversible_tools
    ):
        return "static_ok"
    return "live_required"


class SiblingTest(BaseModel):
    """A positive companion scenario that must continue to pass."""

    test_name: str
    task_fixture: str
    description: str = ""


class RegressionArtifact(BaseModel):
    schema_version: str = REGRESSION_SCHEMA_VERSION
    test_name: str
    source_run_id: str
    # Repo-relative path of the originating task fixture (replay input).
    task_fixture: str
    initial_state: dict[str, Any]
    # The exact docs (content + status + metadata) the failing run saw.
    pinned_docs: list[dict[str, Any]] = Field(default_factory=list)
    # The agent's normalized moves as recorded in the trace, in order. Pinning
    # these makes a replay reproducible even if the fixture script is edited
    # later; empty on artifacts written before 0.2.0, which fall back to the
    # script named in the task fixture.
    pinned_agent_actions: list[dict[str, Any]] = Field(default_factory=list)
    expected_behavior: list[str] = Field(default_factory=list)
    forbidden_actions: list[str] = Field(default_factory=list)
    # Verifier check ids that must hold when this regression is replayed.
    verifier_checks: list[str] = Field(default_factory=list)
    positive_sibling_tests: list[SiblingTest] = Field(default_factory=list)
    severity: Severity
    blocks_release: bool
    replay_mode: ReplayMode = "unlabeled"
    replay_mode_basis: ReplayModeBasis | None = None
    replay_command: str
    metadata: dict[str, Any] = Field(default_factory=dict)
