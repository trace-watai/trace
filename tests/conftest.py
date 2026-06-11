"""Shared test plumbing: run repo fixtures end-to-end into a temp runs dir.

Tests run real fixtures from ``fixtures/`` at the repo root (no synthetic
duplicates that could drift), writing artifacts into pytest temp dirs so the
repo's ``runs/`` directory is never touched by CI.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from trace_harness.environment.support_env import SupportEnvironment
from trace_harness.models.fixture import FixtureModelAdapter
from trace_harness.runner.agent_runner import AgentRunner
from trace_harness.runner.config import RunConfig
from trace_harness.runner.result import RunResult
from trace_harness.tasks.loader import load_docs_for_task, load_task
from trace_harness.tasks.schemas import TaskSpec
from trace_harness.tracing import artifact_store as names
from trace_harness.tracing.artifact_store import ArtifactStore
from trace_harness.tracing.events import TraceEvent

# tests/ -> repo root
REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = REPO_ROOT / "fixtures"
FAILURE_TASK_PATH = FIXTURES_DIR / "tasks" / "refund_policy_failure.json"
VALID_TASK_PATH = FIXTURES_DIR / "tasks" / "refund_policy_valid_cash.json"
EXPECTED_VERIFIER_PATH = FIXTURES_DIR / "expected" / "refund_policy_failure_expected_verifier.json"


@dataclass
class FixtureRun:
    """Everything a test needs from one completed fixture run."""

    task: TaskSpec
    result: RunResult
    store: ArtifactStore
    run_id: str
    trace: list[TraceEvent]
    initial_state: dict[str, Any]
    final_state: dict[str, Any]


def run_task_fixture(task_path: Path, runs_dir: Path) -> FixtureRun:
    """Execute one repo task fixture exactly the way the CLI does."""
    task = load_task(task_path)
    docs = load_docs_for_task(task, task_path)
    script_path = (task_path.parent / task.metadata["fixture_script"]).resolve()
    environment = SupportEnvironment.from_task(task, docs=docs)
    adapter = FixtureModelAdapter.from_file(script_path)
    store = ArtifactStore(runs_dir)
    config = RunConfig(
        task_id=task.task_id,
        provider="fixture",
        model=f"scripted:{script_path.stem}",
        metadata={"task_fixture_path": str(task_path)},
    )
    result = AgentRunner(adapter, environment, store).run(task, config)
    return FixtureRun(
        task=task,
        result=result,
        store=store,
        run_id=result.run_id,
        trace=store.read_trace(result.run_id),
        initial_state=store.read_json(result.run_id, names.INITIAL_STATE),
        final_state=store.read_json(result.run_id, names.FINAL_STATE),
    )


@pytest.fixture
def failure_run(tmp_path: Path) -> FixtureRun:
    return run_task_fixture(FAILURE_TASK_PATH, tmp_path / "runs")


@pytest.fixture
def valid_run(tmp_path: Path) -> FixtureRun:
    return run_task_fixture(VALID_TASK_PATH, tmp_path / "runs")
