"""SuiteReport: what a finished batch actually exercised.

``batch_summary.json`` (``runner/batch.py``) says how many runs passed and how
many failed. It does not say *which* verifier checks fired, which failure
categories the failures fell into, or which failure modes the suite's tasks
*claim* to target that never actually showed up. All of that is already on
disk after a batch — per run, a ``verifier_result.json``,
``attribution_result.json``, ``regression_artifact.json`` and
``task_spec.json``. This module reads them back and rolls them up into one
:class:`SuiteReport` (TRA-90).

Contract
    - **Read-only.** :func:`build_suite_report` never re-runs a task and never
      writes; the CLI and :class:`ArtifactStore` own persistence.
    - **Degrades, never crashes.** A failing run missing its attribution file
      gets category ``unknown`` and a warning (never an exception).
    - **No dashboard concerns.** A TypeScript mirror in the style of
      ``apps/dashboard/src/data/run-loader.ts`` is a later ticket; keep this
      model free of view logic.

Field derivations are documented in ``docs/suite_report.md``.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from trace_harness.attribution.schemas import FailureCategory
from trace_harness.tracing import artifact_store as names
from trace_harness.tracing.artifact_store import ArtifactStore
from trace_harness.tracing.events import utc_now

if TYPE_CHECKING:
    from trace_harness.runner.batch import BatchRunEntry, BatchSummary

SUITE_REPORT_SCHEMA_VERSION = "0.1.0"

# A task's family is the first path segment under this marker; anything not
# below it (the five canonical outcomes) is "canonical".
_TASK_FAMILIES_MARKER = "refund_task_families/"
_CANONICAL_FAMILY = "canonical"
_UNKNOWN_CATEGORY = FailureCategory.UNKNOWN.value


# --------------------------------------------------------------------------
# models
# --------------------------------------------------------------------------


class SuiteReportRow(BaseModel):
    """One batch cell (one task under one agent config), fully resolved.

    ``run_id`` is ``None`` only for a cell whose setup failed before a run
    existed. For a passing (or un-verified) row the failure fields are inert:
    ``primary_failure_category`` is ``""`` and the step / contributing fields
    are empty. That is also true for an ``incomplete`` row that recorded no
    violations before the run died — ``verifier_passed`` is forced ``False``
    for it (see ``verifiers.base.mark_incomplete``), but it is not a verifier
    *failure*, so it gets no category and no missing-attribution warning;
    ``verdict`` is what distinguishes the two.
    """

    run_id: str | None
    task_id: str
    family: str
    agent_label: str
    verifier_passed: bool | None
    # "pass" | "fail" | "incomplete" | null (pre-batch-schema-0.2.0 / unverified).
    verdict: str | None = None
    failed_check_ids: list[str] = Field(default_factory=list)
    severity: str | None = None
    blocks_release: bool = False
    primary_failure_category: str = ""
    contributing_failure_categories: list[str] = Field(default_factory=list)
    root_cause_step: int | None = None
    first_irreversible_action_step: int | None = None
    positive_sibling_task_ids: list[str] = Field(default_factory=list)
    regression_test_name: str | None = None


class SuiteReportTotals(BaseModel):
    """Roll-ups over the rows. Every dict is key-sorted for stable diffs.

    ``by_check_id`` / ``by_failure_category`` count *failing* rows only;
    ``by_family`` / ``by_agent_label`` count every row. ``pass_rate_by_family``
    is over rows that produced a verdict (setup errors excluded).
    """

    by_check_id: dict[str, int] = Field(default_factory=dict)
    by_failure_category: dict[str, int] = Field(default_factory=dict)
    by_family: dict[str, int] = Field(default_factory=dict)
    by_agent_label: dict[str, int] = Field(default_factory=dict)
    pass_rate_by_family: dict[str, float] = Field(default_factory=dict)


class SuiteReportCoverage(BaseModel):
    """The suite's claimed failure modes vs. the categories it actually produced.

    ``claimed_vs_observed`` maps each claimed mode (a ``targeted_failure_modes``
    string) to the sorted set of failure categories observed on failing runs of
    tasks that declared it — an empty list means "claimed, nothing seen".
    ``claimed_never_observed`` / ``observed_never_claimed`` are the two global
    set differences; the ``unknown`` sentinel is never listed as "observed".
    """

    claimed_vs_observed: dict[str, list[str]] = Field(default_factory=dict)
    claimed_never_observed: list[str] = Field(default_factory=list)
    observed_never_claimed: list[str] = Field(default_factory=list)


class SuiteReport(BaseModel):
    """The per-batch report: one row per cell, roll-ups, and coverage."""

    schema_version: str = SUITE_REPORT_SCHEMA_VERSION
    batch_id: str
    suite_id: str
    generated_at: datetime
    total_rows: int
    failing_rows: int
    rows: list[SuiteReportRow]
    totals: SuiteReportTotals
    coverage: SuiteReportCoverage
    warnings: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------


def family_for_task_path(task_path: str) -> str:
    """First folder under ``refund_task_families/``; ``canonical`` otherwise."""
    normalized = task_path.replace("\\", "/")
    if _TASK_FAMILIES_MARKER not in normalized:
        return _CANONICAL_FAMILY
    tail = normalized.split(_TASK_FAMILIES_MARKER, 1)[1]
    return tail.split("/", 1)[0] or _CANONICAL_FAMILY


def _read_optional_dict(store: ArtifactStore, run_id: str, name: str) -> dict | None:
    """Read one run artifact as a dict, or ``None`` if it is absent/not an object."""
    try:
        data = store.read_json(run_id, name)
    except FileNotFoundError:
        return None
    return data if isinstance(data, dict) else None


class _CoverageAccumulator:
    """Threaded through row building so coverage is a single pass over the batch."""

    def __init__(self) -> None:
        self.claimed: set[str] = set()
        self.observed: set[str] = set()
        self.per_mode: dict[str, set[str]] = {}

    def record(self, targeted_modes: list[str], row_categories: set[str]) -> None:
        self.claimed.update(targeted_modes)
        seen = {c for c in row_categories if c and c != _UNKNOWN_CATEGORY}
        self.observed.update(seen)
        for mode in targeted_modes:
            self.per_mode.setdefault(mode, set()).update(seen)


def _task_spec_fields(spec: dict | None) -> tuple[list[str], list[str]]:
    """``(targeted_failure_modes, positive_sibling_task_ids)`` from ``task_spec.json``."""
    if spec is None:
        return [], []
    modes = [m for m in spec.get("targeted_failure_modes", []) if isinstance(m, str)]
    siblings: list[str] = []
    for sibling in (spec.get("metadata") or {}).get("positive_sibling_tasks", []) or []:
        fixture = (sibling or {}).get("task_fixture")
        if isinstance(fixture, str):
            siblings.append(Path(fixture).stem)
    return modes, siblings


def _verifier_fields(
    verifier: dict | None, fallback_severity: str | None
) -> tuple[list[str], str | None, bool]:
    """``(sorted failed check ids, severity, blocks_release)`` from ``verifier_result.json``."""
    if verifier is None:
        return [], fallback_severity, False
    failed_check_ids = sorted(
        check["check_id"]
        for check in verifier.get("failed_checks", [])
        if isinstance(check, dict) and isinstance(check.get("check_id"), str)
    )
    return (
        failed_check_ids,
        verifier.get("severity") or fallback_severity,
        bool(verifier.get("blocks_release", False)),
    )


def _attribution_fields(
    attribution: dict | None,
) -> tuple[str, list[str], int | None, int | None]:
    """``(primary category, contributing, root_cause_step, first_irreversible_step)``.

    A failing run whose attribution file is absent lands here as ``attribution
    is None`` and is recorded as ``unknown`` (the caller emits the warning).
    """
    if attribution is None:
        return _UNKNOWN_CATEGORY, [], None, None
    return (
        attribution.get("primary_failure_category") or _UNKNOWN_CATEGORY,
        [c for c in attribution.get("contributing_failure_categories", []) if isinstance(c, str)],
        attribution.get("root_cause_step"),
        attribution.get("first_irreversible_action_step"),
    )


def _build_row(
    entry: BatchRunEntry, store: ArtifactStore, coverage: _CoverageAccumulator
) -> tuple[SuiteReportRow, list[str]]:
    run_id = entry.run_id
    warnings: list[str] = []
    failed = entry.verifier_passed is False

    task_spec = _read_optional_dict(store, run_id, names.TASK_SPEC) if run_id else None
    if run_id is not None and task_spec is None:
        warnings.append(
            f"{entry.task_id}: task_spec.json missing; "
            "targeted_failure_modes and positive siblings omitted for this row"
        )
    targeted_modes, sibling_ids = _task_spec_fields(task_spec)

    verifier = _read_optional_dict(store, run_id, names.VERIFIER_RESULT) if run_id else None
    failed_check_ids, severity, blocks_release = _verifier_fields(verifier, entry.severity)

    # A blocking violation is what makes a row worth explaining — not merely
    # verifier_passed is False. An `incomplete` run (died before finishing;
    # verifiers.base.mark_incomplete forces passed=False) that recorded zero
    # violations has nothing to attribute, and attribute/bundle correctly
    # never ran for it: that is not a gap, so it gets no category and no
    # warning, only its `verdict`. An incomplete run that *did* record a
    # violation before dying is attributed exactly like a normal failure.
    has_violations = bool(failed_check_ids)
    attribution = (
        _read_optional_dict(store, run_id, names.ATTRIBUTION_RESULT)
        if run_id and failed and has_violations
        else None
    )
    if failed and has_violations and attribution is None:
        warnings.append(
            f"{entry.task_id}: verifier failed but attribution_result.json is missing; "
            f"failure category recorded as '{_UNKNOWN_CATEGORY}'"
        )
    if failed and has_violations:
        primary_category, contributing, root_cause_step, first_irreversible_step = (
            _attribution_fields(attribution)
        )
    else:
        primary_category, contributing = "", []
        root_cause_step, first_irreversible_step = None, None

    regression = _read_optional_dict(store, run_id, names.REGRESSION_ARTIFACT) if run_id else None
    regression_test_name = (
        regression["test_name"]
        if regression is not None and isinstance(regression.get("test_name"), str)
        else None
    )

    coverage.record(targeted_modes, {primary_category, *contributing} if failed else set())

    row = SuiteReportRow(
        run_id=run_id,
        task_id=entry.task_id,
        family=family_for_task_path(entry.task_path),
        agent_label=entry.agent_label,
        verifier_passed=entry.verifier_passed,
        verdict=entry.verdict,
        failed_check_ids=failed_check_ids,
        severity=severity,
        blocks_release=blocks_release,
        primary_failure_category=primary_category,
        contributing_failure_categories=contributing,
        root_cause_step=root_cause_step,
        first_irreversible_action_step=first_irreversible_step,
        positive_sibling_task_ids=sibling_ids,
        regression_test_name=regression_test_name,
    )
    return row, warnings


def _build_totals(rows: list[SuiteReportRow]) -> SuiteReportTotals:
    by_check: Counter[str] = Counter()
    by_category: Counter[str] = Counter()
    by_family: Counter[str] = Counter()
    by_agent: Counter[str] = Counter()
    family_verdicts: dict[str, list[bool]] = {}

    for row in rows:
        by_family[row.family] += 1
        by_agent[row.agent_label] += 1
        for check_id in row.failed_check_ids:
            by_check[check_id] += 1
        if row.verifier_passed is False:
            by_category[row.primary_failure_category or _UNKNOWN_CATEGORY] += 1
        if row.verifier_passed is not None:
            family_verdicts.setdefault(row.family, []).append(row.verifier_passed)

    pass_rate = {
        family: round(sum(verdicts) / len(verdicts), 4)
        for family, verdicts in sorted(family_verdicts.items())
        if verdicts
    }
    return SuiteReportTotals(
        by_check_id=dict(sorted(by_check.items())),
        by_failure_category=dict(sorted(by_category.items())),
        by_family=dict(sorted(by_family.items())),
        by_agent_label=dict(sorted(by_agent.items())),
        pass_rate_by_family=pass_rate,
    )


def _build_coverage(coverage: _CoverageAccumulator) -> SuiteReportCoverage:
    return SuiteReportCoverage(
        claimed_vs_observed={
            mode: sorted(coverage.per_mode.get(mode, set())) for mode in sorted(coverage.claimed)
        },
        claimed_never_observed=sorted(coverage.claimed - coverage.observed),
        observed_never_claimed=sorted(coverage.observed - coverage.claimed),
    )


def build_suite_report(summary: BatchSummary, store: ArtifactStore) -> SuiteReport:
    """Roll a finished batch's on-disk artifacts into a :class:`SuiteReport`.

    Reads ``batch_summary.json``'s entries and, per run, its
    ``task_spec.json`` / ``verifier_result.json`` / ``attribution_result.json``
    / ``regression_artifact.json``. Never re-runs anything and never writes.
    """
    coverage = _CoverageAccumulator()
    rows: list[SuiteReportRow] = []
    warnings: list[str] = []
    for entry in summary.entries:
        row, row_warnings = _build_row(entry, store, coverage)
        rows.append(row)
        warnings.extend(row_warnings)

    return SuiteReport(
        batch_id=summary.batch_id,
        suite_id=summary.suite_id,
        generated_at=utc_now(),
        total_rows=len(rows),
        failing_rows=sum(1 for row in rows if row.verifier_passed is False),
        rows=rows,
        totals=_build_totals(rows),
        coverage=_build_coverage(coverage),
        warnings=warnings,
    )


# --------------------------------------------------------------------------
# markdown rendering
# --------------------------------------------------------------------------


def _verdict(row: SuiteReportRow) -> str:
    if row.verdict == "incomplete":
        return "INCOMPLETE"
    if row.verifier_passed is True:
        return "PASS"
    if row.verifier_passed is False:
        return "FAIL"
    return "—"


def _cell(value: object) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value) if value else "—"
    return str(value)


def _counts_table(title: str, counts: dict[str, int | float]) -> list[str]:
    lines = [f"### {title}", "", "| key | count |", "| --- | --- |"]
    if not counts:
        lines.append("| _(none)_ | — |")
    for key, value in counts.items():
        lines.append(f"| `{key}` | {value} |")
    lines.append("")
    return lines


def render_suite_report_markdown(report: SuiteReport) -> str:
    """A three-section markdown rendering: rows, totals, coverage."""
    out: list[str] = [
        f"# Suite report — {report.suite_id}",
        "",
        f"Batch `{report.batch_id}` · generated {report.generated_at.isoformat()} · "
        f"{report.total_rows} rows, {report.failing_rows} failing",
        "",
        "## Rows",
        "",
        (
            "| run | task | family | agent | verdict | checks fired | severity | blocks | "
            "primary category | contributing | root step | first irreversible | "
            "positive siblings | regression test |"
        ),
        "| " + " | ".join(["---"] * 14) + " |",
    ]
    for row in report.rows:
        out.append(
            "| "
            + " | ".join(
                [
                    _cell(row.run_id),
                    row.task_id,
                    row.family,
                    row.agent_label,
                    _verdict(row),
                    _cell(row.failed_check_ids),
                    _cell(row.severity),
                    "yes" if row.blocks_release else "no",
                    _cell(row.primary_failure_category),
                    _cell(row.contributing_failure_categories),
                    _cell(row.root_cause_step),
                    _cell(row.first_irreversible_action_step),
                    _cell(row.positive_sibling_task_ids),
                    _cell(row.regression_test_name),
                ]
            )
            + " |"
        )

    out += ["", "## Totals", ""]
    out += _counts_table("Runs per check id", report.totals.by_check_id)
    out += _counts_table("Failing rows per failure category", report.totals.by_failure_category)
    out += _counts_table("Rows per family", report.totals.by_family)
    out += _counts_table("Rows per agent label", report.totals.by_agent_label)
    out += _counts_table("Pass rate per family", report.totals.pass_rate_by_family)

    out += [
        "## Coverage",
        "",
        "### Claimed failure modes vs. observed categories",
        "",
        "| claimed mode | observed categories |",
        "| --- | --- |",
    ]
    for mode, observed in report.coverage.claimed_vs_observed.items():
        out.append(f"| `{mode}` | {_cell(observed)} |")
    out += [
        "",
        "### Claimed but never observed",
        "",
        *(
            [f"- `{mode}`" for mode in report.coverage.claimed_never_observed]
            or ["_(none — every claimed mode showed up)_"]
        ),
        "",
        "### Observed but never claimed",
        "",
        *(
            [f"- `{category}`" for category in report.coverage.observed_never_claimed]
            or ["_(none)_"]
        ),
        "",
    ]

    if report.warnings:
        out += ["## Warnings", "", *(f"- {warning}" for warning in report.warnings), ""]

    return "\n".join(out) + "\n"
