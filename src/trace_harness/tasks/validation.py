"""Authoring-quality validation for task fixtures — the "is this a *good* task?" layer.

Distinct from the *structural* schema in ``schemas.py``:
    - ``TaskSpec.model_validate`` (schema) guarantees a task is well-*formed* and
      stays deliberately lenient, so other modules can build minimal stub tasks
      in their unit tests.
    - ``validate_task`` (here) judges whether a well-formed task is actually fit
      to evaluate an agent on: non-empty tools/verifiers/failure-modes, defines
      correct behavior, deterministic state (no clocks), multi-step, etc.

These stricter rules live here (not in the model) and run only against real
fixtures and the CLI/CI hook below — never inside ``TaskSpec`` — so they cannot
break the stub tasks other teams construct.

Run as a CLI to validate every committed task fixture:
    python -m trace_harness.tasks.validation

Exits non-zero if any seed task has errors, or if a task marked
``metadata.expected_invalid = true`` unexpectedly passes (the counterexample
guard). Authoring counterexamples opt out of "must pass" by setting that flag.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from trace_harness.attribution.schemas import FailureCategory
from trace_harness.tasks.loader import TaskLoadError, load_task
from trace_harness.tasks.schemas import TaskSpec
from trace_harness.verifiers.registry import available_verifier_ids

Severity = Literal["error", "warning"]

# Canonical agent-failure vocabulary, owned by Darrel (attribution). Task
# `targeted_failure_modes` should draw from this so task labels join with the
# Judge's failure_category. See docs/failure_taxonomy.md.
_FAILURE_CATEGORIES = {category.value for category in FailureCategory}

# Vague instructions can't be objectively verified, the agent has no concrete target.
_VAGUE_TERMS = re.compile(
    r"\b(do right by|appropriately|as needed|handle it|as you see fit|"
    r"use your best judg(?:e)?ment|do the right thing)\b",
    re.IGNORECASE,
)
# An ISO-8601-ish absolute date embedded in state is a wall clock; fixtures must encode
# time as relative ages (e.g. purchase_age_days) so runs are reproducible and consistent.
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_CLOCK_KEYS = {"current_date", "now", "today", "timestamp", "datetime"}


@dataclass(frozen=True)
class ValidationIssue:
    """One authoring-quality finding. ``severity='error'`` blocks a seed task."""

    code: str
    message: str
    severity: Severity = "error"


def _find_clocks(value: Any, path: str = "initial_state") -> list[str]:
    """Recursively collect dotted paths whose key or value looks like wall-clock time."""
    hits: list[str] = []
    if isinstance(value, dict):
        for key, sub in value.items():
            if key in _CLOCK_KEYS:
                hits.append(f"{path}.{key}")
            hits.extend(_find_clocks(sub, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, sub in enumerate(value):
            hits.extend(_find_clocks(sub, f"{path}[{index}]"))
    elif isinstance(value, str) and _ISO_DATE.search(value):
        hits.append(path)
    return hits


def validate_task(task: TaskSpec) -> list[ValidationIssue]:
    """Return authoring-quality issues for a structurally valid task (empty == good)."""
    issues: list[ValidationIssue] = []

    if not task.available_tools:
        issues.append(
            ValidationIssue(
                "empty_available_tools",
                "available_tools is empty; the agent has nothing to act with",
            )
        )
    if not task.verifier_ids:
        issues.append(
            ValidationIssue(
                "empty_verifier_ids",
                "verifier_ids is empty; nothing can decide pass/fail for this task",
            )
        )
    else:
        unknown = [vid for vid in task.verifier_ids if vid not in available_verifier_ids()]
        if unknown:
            issues.append(
                ValidationIssue(
                    "unknown_verifier_id",
                    f"verifier_ids reference unregistered verifiers {unknown}; "
                    f"registered: {available_verifier_ids()}",
                )
            )
    if not task.targeted_failure_modes:
        issues.append(
            ValidationIssue(
                "empty_targeted_failure_modes",
                "targeted_failure_modes is empty; the task exposes no known failure",
            )
        )
    else:
        unknown_modes = [m for m in task.targeted_failure_modes if m not in _FAILURE_CATEGORIES]
        if unknown_modes:
            issues.append(
                ValidationIssue(
                    "unknown_failure_mode",
                    f"targeted_failure_modes {unknown_modes} are not in the FailureCategory "
                    "taxonomy (attribution)",
                    severity="warning",
                )
            )
    if not task.expected_behavior and not task.forbidden_actions:
        issues.append(
            ValidationIssue(
                "no_correct_behavior",
                "neither expected_behavior nor forbidden_actions is set; "
                "correct behavior is undefined",
            )
        )

    clocks = _find_clocks(task.initial_state)
    if clocks:
        issues.append(
            ValidationIssue(
                "clock_in_initial_state",
                f"initial_state contains wall-clock time ({', '.join(clocks)}); "
                "use relative ages (e.g. purchase_age_days) for reproducibility",
            )
        )

    for field_name in ("goal", "description"):
        match = _VAGUE_TERMS.search(getattr(task, field_name))
        if match:
            issues.append(
                ValidationIssue(
                    "vague_language",
                    f"{field_name} contains vague wording '{match.group()}'; "
                    "state the concrete intended outcome",
                    severity="warning",
                )
            )

    if len(task.available_tools) < 2:
        issues.append(
            ValidationIssue(
                "not_multi_step",
                "fewer than 2 available_tools; tasks should require multi-step behavior",
                severity="warning",
            )
        )
    if not task.required_evidence:
        issues.append(
            ValidationIssue(
                "missing_required_evidence",
                "required_evidence is empty; reviewers/verifiers have no evidence manifest",
                severity="warning",
            )
        )

    return issues


def errors(issues: list[ValidationIssue]) -> list[ValidationIssue]:
    """Filter to error-severity issues (the ones that block a seed task)."""
    return [issue for issue in issues if issue.severity == "error"]


def validate_task_file(path: Path) -> list[ValidationIssue]:
    """Load and authoring-validate one task file; structural load errors surface too."""
    try:
        task = load_task(path)
    except TaskLoadError as exc:
        return [ValidationIssue("structural_invalid", str(exc))]
    return validate_task(task)


def _report(path: Path, *, expect_invalid: bool) -> bool:
    """Validate one fixture, print its status + issues, return whether it's as expected."""
    issues = validate_task(load_task(path))
    errs = errors(issues)
    if expect_invalid:
        passed = bool(errs)  # a counterexample is "correct" only when it IS flagged
        status = "ok (counterexample flagged)" if passed else "FAIL (no longer flagged)"
    else:
        passed = not errs
        status = "ok" if passed else "FAIL"
    print(f"{path.name}: {status}")
    for issue in issues:
        print(f"    [{issue.severity}] {issue.code}: {issue.message}")
    return passed


def _main() -> int:
    """Seed tasks (top level) must be clean; counterexamples (subfolder) must be flagged."""
    tasks_dir = Path(__file__).resolve().parents[3] / "fixtures" / "tasks"
    all_ok = True
    for path in sorted(tasks_dir.glob("*.json")):
        all_ok = _report(path, expect_invalid=False) and all_ok
    for path in sorted((tasks_dir / "counterexamples").glob("*.json")):
        all_ok = _report(path, expect_invalid=True) and all_ok
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(_main())
