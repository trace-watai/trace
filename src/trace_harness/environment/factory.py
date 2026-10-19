"""The single workflow-to-environment lookup, including pinned replay worlds."""

from __future__ import annotations

from pathlib import Path

from trace_harness.environment.retrieval_env import RetrievalEnvironment
from trace_harness.environment.state import Doc
from trace_harness.environment.support_env import SupportEnvironment
from trace_harness.tasks.schemas import TaskSpec

ENVIRONMENT_FACTORIES = {
    "support.refund": SupportEnvironment.from_task,
    "retrieval.answer": RetrievalEnvironment.from_task,
}


def create_environment(
    task: TaskSpec,
    docs: list[Doc] | None = None,
    control_library: Path | str | None = None,
) -> SupportEnvironment | RetrievalEnvironment:
    try:
        factory = ENVIRONMENT_FACTORIES[task.workflow_type]
    except KeyError:
        raise ValueError(f"unknown workflow_type {task.workflow_type!r}") from None
    return factory(task, docs=docs, control_library=control_library)
