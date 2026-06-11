"""Turn a verified failure into a pinned, rerunnable regression artifact.

What gets pinned and why
    - ``initial_state`` and ``pinned_docs`` come from the run's recorded
      initial state, not from the live fixture files — so the regression
      keeps testing the world the failure actually happened in, even if
      fixtures evolve later.
    - ``verifier_checks`` are the check ids that failed; replaying the
      regression means asserting these checks (still) hold on a fixed agent.
    - ``positive_sibling_tests`` come from the task's
      ``metadata.positive_sibling_tasks`` so every blocking regression is
      paired with at least one scenario that must keep passing.

Replay (MVP honesty)
    ``replay_command`` re-runs the originating task fixture through the
    pipeline. The artifact itself is already self-contained (state + docs),
    but a ``trace-harness replay <regression_artifact.json>`` command that
    consumes it directly does not exist yet.

# TODO(Samir/regression): add the replay-from-artifact command and a CI
# collector that sweeps runs/ (or a curated regression dir) and executes
# every blocking regression + its positive siblings.
"""

from __future__ import annotations

from typing import Any

from trace_harness.regression.schemas import RegressionArtifact, SiblingTest
from trace_harness.tasks.schemas import TaskSpec
from trace_harness.verifiers.base import VerifierResult


def materialize_regression_artifact(
    *,
    task: TaskSpec,
    verifier_result: VerifierResult,
    initial_state: dict[str, Any],
    run_id: str,
    task_fixture_path: str | None,
) -> RegressionArtifact:
    """Build a :class:`RegressionArtifact` from one verified failure."""
    if verifier_result.passed:
        raise ValueError("cannot materialize a regression from a passing run")

    fixture_path = task_fixture_path or f"runs/{run_id}/task_spec.json"
    siblings = [
        SiblingTest.model_validate(entry)
        for entry in task.metadata.get("positive_sibling_tasks", [])
    ]

    return RegressionArtifact(
        test_name=f"regression_{task.task_id}",
        source_run_id=run_id,
        task_fixture=fixture_path,
        initial_state=initial_state,
        pinned_docs=list(initial_state.get("docs", [])),
        expected_behavior=task.expected_behavior,
        forbidden_actions=task.forbidden_actions,
        # Order-preserving dedupe: one check class can fail on N records.
        verifier_checks=list(
            dict.fromkeys(check.check_id for check in verifier_result.failed_checks)
        ),
        positive_sibling_tests=siblings,
        severity=verifier_result.severity or task.severity,
        blocks_release=verifier_result.blocks_release,
        replay_command=f"trace-harness run-pipeline {fixture_path}",
        metadata={
            "source_verifier_id": verifier_result.verifier_id,
            "note": (
                "replay_command re-runs the originating fixture; replay directly "
                "from this artifact's pinned state is not implemented yet"
            ),
        },
    )
