"""Loading task fixtures (and their referenced doc fixtures) from disk.

Why this exists
    Fixtures are plain JSON so that non-Python contributors (task design,
    dashboard) can author and read them. This module is the single place
    where JSON becomes validated Pydantic objects, with errors that point at
    the offending file.

Assumptions
    - ``TaskSpec.docs_fixture`` is resolved relative to the *task file's*
      directory, so fixtures can move together as a folder.
    - A docs fixture file has the shape
      ``{"schema_version": ..., "collection_id": ..., "docs": [Doc, ...]}``.

# TODO(Emily/tasks): add a `validate-fixtures` CLI hook that loads every
# fixture under fixtures/ so CI catches schema drift in task JSON.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from trace_harness.tasks.schemas import TaskSpec

if TYPE_CHECKING:
    from trace_harness.environment.state import Doc


class TaskLoadError(ValueError):
    """Raised when a task or docs fixture file is missing or invalid."""


def _read_json(path: Path) -> Any:
    if not path.is_file():
        raise TaskLoadError(f"fixture file not found: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TaskLoadError(f"invalid JSON in {path}: {exc}") from exc


def load_task(path: Path | str) -> TaskSpec:
    """Load and validate a :class:`TaskSpec` from a JSON file."""
    task_path = Path(path)
    data = _read_json(task_path)
    try:
        return TaskSpec.model_validate(data)
    except ValidationError as exc:
        raise TaskLoadError(f"invalid task spec in {task_path}:\n{exc}") from exc


def load_docs_for_task(task: TaskSpec, task_path: Path | str) -> list[Doc]:
    """Load the docs selected by ``task.available_docs`` from ``task.docs_fixture``.

    Returns an empty list when the task declares no docs fixture (the
    environment may still receive inline docs via ``initial_state["docs"]``).
    Raises :class:`TaskLoadError` if the task references a doc_id the fixture
    does not contain — a task authoring bug we want to surface immediately.
    """
    # Imported here (not at module top) to keep tasks.schemas a dependency
    # leaf: environment.state imports TaskSpec for type hints, and a runtime
    # import cycle would otherwise be one careless refactor away.
    from trace_harness.environment.state import Doc

    if task.docs_fixture is None:
        return []

    docs_path = (Path(task_path).parent / task.docs_fixture).resolve()
    data = _read_json(docs_path)
    raw_docs = data.get("docs")
    if not isinstance(raw_docs, list):
        raise TaskLoadError(f"docs fixture {docs_path} must contain a top-level 'docs' list")

    try:
        by_id = {doc["doc_id"]: Doc.model_validate(doc) for doc in raw_docs}
    except (KeyError, ValidationError) as exc:
        raise TaskLoadError(f"invalid doc entry in {docs_path}: {exc}") from exc

    missing = [doc_id for doc_id in task.available_docs if doc_id not in by_id]
    if missing:
        raise TaskLoadError(
            f"task '{task.task_id}' references doc ids {missing} "
            f"that are not present in {docs_path}"
        )
    return [by_id[doc_id] for doc_id in task.available_docs]
