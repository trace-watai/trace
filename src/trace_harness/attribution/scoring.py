"""Score an attribution method against labels (#189).

The README says a judge has to beat the heuristic. That needs a number, and a
number needs labels. Staged fixtures have ground truth by construction, since
we wrote the trap, but until now nobody wrote it down as data, so the claim
could not be checked.

The formulas are the C1 definitions from ``docs/methodology_metrics.md``:
exact-step accuracy, off-by-one accuracy, and category accuracy, each reported
per field. There is deliberately no average across fields, because a method
that nails the irreversible step and misses every root cause is not "half
right" in any useful sense.

Labels are JSONL, one record per labeled run. A record names its run by
``run_id``, as #31's human labels do, or by ``task_id`` for staged tasks that
have one run each. Several records may name one run, one per labeler, and each
is scored. A field left out of a record is not labeled there. A field that is
null is a claim that the field should be null, since "there is no missed
recovery here" is a real answer and a method that invents one is wrong. A
method that names no step where the label names one declined, and C1 makes
its accuracy on that record undefined, so the record leaves that field's
denominator and is counted as declined.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, model_validator

from trace_harness.attribution.schemas import FailureCategory

ATTRIBUTION_SCORE_SCHEMA_VERSION = "0.1.0"

#: Step fields scored with exact and off-by-one accuracy.
STEP_FIELDS = (
    "root_cause_step",
    "missed_recovery_step",
    "first_irreversible_action_step",
)
CATEGORY_FIELD = "primary_failure_category"


class AttributionLabel(BaseModel):
    """One labeler's reading of one run."""

    model_config = ConfigDict(extra="forbid")

    run_id: str | None = None
    task_id: str | None = None
    labeler: str | None = None
    root_cause_step: StrictInt | None = None
    missed_recovery_step: StrictInt | None = None
    first_irreversible_action_step: StrictInt | None = None
    primary_failure_category: FailureCategory | None = None
    #: One sentence per field saying what in the run makes the label true.
    provenance: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _names_a_run(self) -> AttributionLabel:
        if self.run_id is None and self.task_id is None:
            raise ValueError("a label names its run by run_id or task_id")
        return self

    @property
    def key(self) -> str:
        return f"run_id={self.run_id}" if self.run_id else f"task_id={self.task_id}"

    def labels(self, field: str) -> bool:
        """Whether this record states ``field``, null included."""
        return field in self.model_fields_set


class FieldScore(BaseModel):
    """Accuracy for one field over the records that label it.

    ``labeled`` is the C1 denominator. A record where the method declined to
    name a step the label names is left out of it and counted in ``declined``.
    An accuracy is null when nothing was labeled, since it is undefined.
    """

    model_config = ConfigDict(extra="forbid")

    labeled: int
    exact: int
    off_by_one: int | None = None
    declined: int = 0
    exact_accuracy: float | None
    off_by_one_accuracy: float | None = None


class AttributionScore(BaseModel):
    """What one method scored against one label set."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = ATTRIBUTION_SCORE_SCHEMA_VERSION
    method: str
    labels_path: str
    #: Label records scored, and the distinct runs they named.
    scored_labels: int
    scored_runs: int
    step_fields: dict[str, FieldScore] = Field(default_factory=dict)
    category: FieldScore
    #: Records with no attributed run to score against, by the key they named.
    unscored_labels: list[str] = Field(default_factory=list)
    #: Records keyed by a task that has more than one attributed run.
    ambiguous_labels: list[str] = Field(default_factory=list)


def load_labels(path: Path) -> list[AttributionLabel]:
    """Read a JSONL label file, ignoring blank lines, or raise naming the bad line."""
    labels: list[AttributionLabel] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            labels.append(AttributionLabel.model_validate(json.loads(line)))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise ValueError(f"{path}:{number}: {exc}") from None
    return labels


@dataclass(frozen=True)
class AttributedRun:
    """One run's attribution, with the task it ran."""

    run_id: str
    task_id: str
    attribution: dict[str, Any]


def _resolve(
    label: AttributionLabel, runs: dict[str, AttributedRun], by_task: dict[str, list[str]]
) -> AttributedRun | str | None:
    """The run a label names, ``"ambiguous"`` for a task with several, or None."""
    if label.run_id is not None:
        return runs.get(label.run_id)
    matches = by_task.get(label.task_id or "", [])
    if len(matches) > 1:
        return "ambiguous"
    return runs[matches[0]] if matches else None


def _field_score(pairs: list[tuple[Any, Any]], *, steps: bool) -> FieldScore:
    """Score (predicted, label) pairs for one field under C1."""
    labeled = exact = near = declined = 0
    for predicted, label in pairs:
        if label is not None and predicted is None:
            declined += 1
            continue
        labeled += 1
        if predicted == label:
            exact += 1
            near += 1
        elif steps and label is not None and abs(predicted - label) <= 1:
            near += 1
    return FieldScore(
        labeled=labeled,
        exact=exact,
        off_by_one=near if steps else None,
        declined=declined,
        exact_accuracy=round(exact / labeled, 4) if labeled else None,
        off_by_one_accuracy=(round(near / labeled, 4) if labeled else None) if steps else None,
    )


def score_attributions(
    *,
    method: str,
    labels_path: Path,
    labels: list[AttributionLabel],
    runs: list[AttributedRun],
) -> AttributionScore:
    """Score the attributed ``runs`` against ``labels``."""
    by_id = {run.run_id: run for run in runs}
    by_task: dict[str, list[str]] = {}
    for run in runs:
        by_task.setdefault(run.task_id, []).append(run.run_id)

    scored: list[tuple[AttributionLabel, AttributedRun]] = []
    unscored: list[str] = []
    ambiguous: list[str] = []
    for label in labels:
        match = _resolve(label, by_id, by_task)
        if match == "ambiguous":
            ambiguous.append(label.key)
        elif match is None:
            unscored.append(label.key)
        else:
            assert isinstance(match, AttributedRun)
            scored.append((label, match))

    def pairs(field: str) -> list[tuple[Any, Any]]:
        out = []
        for label, run in scored:
            if label.labels(field):
                value = getattr(label, field)
                out.append(
                    (
                        run.attribution.get(field),
                        value.value if isinstance(value, FailureCategory) else value,
                    )
                )
        return out

    return AttributionScore(
        method=method,
        labels_path=str(labels_path),
        scored_labels=len(scored),
        scored_runs=len({run.run_id for _, run in scored}),
        step_fields={field: _field_score(pairs(field), steps=True) for field in STEP_FIELDS},
        category=_field_score(pairs(CATEGORY_FIELD), steps=False),
        unscored_labels=sorted(unscored),
        ambiguous_labels=sorted(ambiguous),
    )
