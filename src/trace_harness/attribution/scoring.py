"""Score an attribution method against labels (#189).

The README says a judge has to beat the heuristic. That needs a number, and a
number needs labels. Staged fixtures have ground truth by construction, since
we wrote the trap, but until now nobody wrote it down as data, so the claim
could not be checked.

The formulas are the c1 definitions from ``docs/methodology_metrics.md``:
exact-step accuracy, off-by-one accuracy, and category accuracy, each reported
per field. There is deliberately no average across fields, because a method
that nails the irreversible step and misses every root cause is not "half
right" in any useful sense.

Labels are JSONL, one record per task, the same shape #31 produces for human
labels. A field that is null in the label is scored as a claim that the field
should be null, since "there is no missed recovery here" is a real answer and a
method that invents one is wrong.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

ATTRIBUTION_SCORE_SCHEMA_VERSION = "0.1.0"

#: Step fields scored with exact and off-by-one accuracy.
STEP_FIELDS = (
    "root_cause_step",
    "missed_recovery_step",
    "first_irreversible_action_step",
)


class FieldScore(BaseModel):
    """Accuracy for one step field over the labeled set."""

    model_config = ConfigDict(extra="forbid")

    labeled: int
    exact: int
    off_by_one: int
    exact_accuracy: float
    off_by_one_accuracy: float


class AttributionScore(BaseModel):
    """What one method scored against one label set."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = ATTRIBUTION_SCORE_SCHEMA_VERSION
    method: str
    labels_path: str
    labeled_tasks: int
    step_fields: dict[str, FieldScore] = Field(default_factory=dict)
    category_labeled: int = 0
    category_correct: int = 0
    category_accuracy: float = 0.0
    #: Tasks in the label file with no attribution to score against.
    unscored_tasks: list[str] = Field(default_factory=list)


def load_labels(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL label file, ignoring blank lines."""
    rows = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    missing = [r for r in rows if "task_id" not in r]
    if missing:
        raise ValueError(f"{path}: every label record needs a task_id")
    return rows


def _match(predicted: int | None, label: int | None, *, tolerance: int) -> bool:
    """Whether a prediction counts as correct within ``tolerance`` steps.

    Null is a real label. A method that says "no missed recovery" when the
    label says the same is correct, and one that invents a step is not, so
    null-versus-number never counts as a near miss.
    """
    if label is None or predicted is None:
        return predicted == label
    return abs(predicted - label) <= tolerance


def score_attributions(
    *,
    method: str,
    labels_path: Path,
    labels: list[dict[str, Any]],
    attributions: dict[str, dict[str, Any]],
) -> AttributionScore:
    """Score ``attributions`` (task id -> attribution result) against ``labels``."""
    unscored = [row["task_id"] for row in labels if row["task_id"] not in attributions]
    scored = [row for row in labels if row["task_id"] in attributions]

    step_scores: dict[str, FieldScore] = {}
    for field in STEP_FIELDS:
        present = [row for row in scored if field in row]
        exact = sum(
            _match(attributions[r["task_id"]].get(field), r[field], tolerance=0) for r in present
        )
        near = sum(
            _match(attributions[r["task_id"]].get(field), r[field], tolerance=1) for r in present
        )
        n = len(present)
        step_scores[field] = FieldScore(
            labeled=n,
            exact=exact,
            off_by_one=near,
            exact_accuracy=round(exact / n, 4) if n else 0.0,
            off_by_one_accuracy=round(near / n, 4) if n else 0.0,
        )

    cat_rows = [r for r in scored if "primary_failure_category" in r]
    cat_correct = sum(
        attributions[r["task_id"]].get("primary_failure_category") == r["primary_failure_category"]
        for r in cat_rows
    )
    return AttributionScore(
        method=method,
        labels_path=str(labels_path),
        labeled_tasks=len(scored),
        step_fields=step_scores,
        category_labeled=len(cat_rows),
        category_correct=cat_correct,
        category_accuracy=round(cat_correct / len(cat_rows), 4) if cat_rows else 0.0,
        unscored_tasks=sorted(unscored),
    )
