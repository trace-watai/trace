"""Schemas for regression artifacts.

A regression artifact pins everything needed to re-test a failure class
later: the task, the initial state, the exact docs the agent saw, the
checks that must hold, and a replay command. It also carries *positive
sibling tests* — nearby scenarios that must keep PASSING — so the fix for a
failure cannot quietly overblock legitimate behavior.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from trace_harness.tasks.schemas import Severity

REGRESSION_SCHEMA_VERSION = "0.1.0"


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
    expected_behavior: list[str] = Field(default_factory=list)
    forbidden_actions: list[str] = Field(default_factory=list)
    # Verifier check ids that must hold when this regression is replayed.
    verifier_checks: list[str] = Field(default_factory=list)
    positive_sibling_tests: list[SiblingTest] = Field(default_factory=list)
    severity: Severity
    blocks_release: bool
    replay_command: str
    metadata: dict[str, Any] = Field(default_factory=dict)
