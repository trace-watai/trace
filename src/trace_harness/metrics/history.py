"""Three trend measures, appended once per merge to main (#207).

Coverage, over-blocking and cost of learning are trends. A single number for
any of them says nothing, because the question each answers is whether the
library is getting better or worse as commits land. Nothing recorded any of
them across time, so every claim about direction was an impression.

Each measure is read out of artifacts that already exist rather than
recomputed. Over-blocking comes from #146's ``repair_validation.json``, so the
number in a snapshot is the same number the validation gate acted on, and its
family counts and upper bound come from the same function that writes that
file's rollup. Cost of
learning is summed from those validations' re-run directories, counting
irreversible tool calls and the money their final state shows moved.

Every measure carries its denominator and a snapshot never reports a rate for
an empty one. ``0 / 0`` written as ``0.0`` reads as total failure when the
truth is that nothing was measured, and that distinction is the whole point of
keeping a history.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from trace_harness.environment.controls import MATERIALIZABLE_REPAIR_CONTROLS
from trace_harness.metrics.bounds import clopper_pearson_upper
from trace_harness.regression.repair_validation import (
    ControlVerdict,
    RepairValidation,
    over_blocking_summary,
)
from trace_harness.tracing.artifact_store import (
    FINAL_STATE,
    REPAIR_PACKAGE,
    REPAIR_VALIDATION,
    TRACE,
)
from trace_harness.tracing.events import utc_now

# 0.2.0: coverage splits accepted controls into gating and advisory.
# 0.3.0: over-blocking adds task-family counts and a 95% upper bound.
METRICS_SNAPSHOT_SCHEMA_VERSION = "0.3.0"

#: Default history file. One JSON object per line, appended, never rewritten.
HISTORY_PATH = Path("docs/acceptance/metrics_history.jsonl")

#: Side effect classes that cannot be undone by re-running anything.
IRREVERSIBLE_SIDE_EFFECT = "external_irreversible"


class Ratio(BaseModel):
    """A rate that always carries the two counts it came from.

    ``value`` is null on an empty denominator. A plotted zero and a plotted
    gap mean different things and the page is allowed to show the difference.
    """

    model_config = ConfigDict(extra="forbid")

    numerator: int = Field(ge=0)
    denominator: int = Field(ge=0)

    @model_validator(mode="before")
    @classmethod
    def drop_serialized_value(cls, data: Any) -> Any:
        """Ignore a ``value`` read back from a history line.

        ``value`` is written out so the page and anything reading the JSONL
        gets the rate without dividing, but it is always derived. Accepting it
        on input would let a hand-edited history file carry a rate that
        disagrees with its own two counts.
        """
        if isinstance(data, dict) and "value" in data:
            data = {k: v for k, v in data.items() if k != "value"}
        return data

    @computed_field  # type: ignore[prop-decorator]
    @property
    def value(self) -> float | None:
        if self.denominator == 0:
            return None
        return round(self.numerator / self.denominator, 4)


class Coverage(BaseModel):
    """How far a prescribed control gets before it is worth anything.

    A repair package prescribes a control by name. Most names have no
    registered guardrail, so they can never be installed, and a name that can
    be installed still has to survive validation. Reporting only the last
    number would hide which of the three walls the work is stuck behind.

    An accepted name is gating when at least one of its accepted verdicts was
    reached against a ``static_ok`` artifact, and advisory otherwise. The
    split exists because ADR-0002 lets only the first kind gate anything.
    """

    model_config = ConfigDict(extra="forbid")

    prescribed: int = Field(ge=0)
    materializable: int = Field(ge=0)
    validated: int = Field(ge=0)
    accepted: int = Field(ge=0)
    accepted_gating: int = Field(ge=0)
    accepted_advisory: int = Field(ge=0)
    accepted_over_prescribed: Ratio
    materializable_over_prescribed: Ratio
    #: Prescribed names with no entry in the materializability map at all.
    unmapped_controls: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def split_unrecorded_acceptance(cls, data: Any) -> Any:
        """Read a 0.1.0 record, which has no gating and advisory split.

        Validations written before the split did not record a replay_mode, and
        such a verdict reads as unlabeled, which is advisory. The record was
        computed from those validations, so every accepted name in it is
        advisory under the same rule that reads the validations themselves.
        """
        if (
            isinstance(data, dict)
            and "accepted_gating" not in data
            and "accepted_advisory" not in data
        ):
            data = {**data, "accepted_gating": 0, "accepted_advisory": data.get("accepted")}
        return data

    @model_validator(mode="after")
    def split_sums_to_accepted(self) -> Coverage:
        if self.accepted_gating + self.accepted_advisory != self.accepted:
            raise ValueError("gating and advisory acceptances must sum to accepted")
        return self


class OverBlocking(BaseModel):
    """Positive siblings that failed while a control was installed.

    Read from the validation artifacts rather than recomputed, so a snapshot
    cannot disagree with the gate that let the control through.

    ``rate`` counts siblings. The bound counts task families, because
    siblings in one family share a template and mechanism and are not
    independent draws (see ``OverBlockingSummary``). ``0 / 1`` families gives
    a bound of 95%, which is what one clean sibling actually establishes.
    """

    model_config = ConfigDict(extra="forbid")

    siblings_run: int = Field(ge=0)
    siblings_failed: int = Field(ge=0)
    rate: Ratio
    #: Distinct task families among completed siblings, and how many had a
    #: failing sibling. None on records written before 0.3.0.
    independent_families: int | None = Field(default=None, ge=0)
    families_failed: int | None = Field(default=None, ge=0)
    #: Validation artifacts the counts came from, relative to the runs root.
    sources: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def drop_serialized_bound(cls, data: Any) -> Any:
        """Ignore an ``upper_bound_95`` read back from a history line.

        Derived from the family counts on every read, for the reason
        ``Ratio`` drops ``value``.
        """
        if isinstance(data, dict) and "upper_bound_95" in data:
            data = {k: v for k, v in data.items() if k != "upper_bound_95"}
        return data

    @model_validator(mode="after")
    def families_are_consistent(self) -> OverBlocking:
        if (self.independent_families is None) != (self.families_failed is None):
            raise ValueError("independent_families and families_failed are recorded together")
        if (
            self.families_failed is not None
            and self.independent_families is not None
            and self.families_failed > self.independent_families
        ):
            raise ValueError("families_failed cannot exceed independent_families")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def upper_bound_95(self) -> float | None:
        """One-sided 95% Clopper-Pearson bound on the family failure rate."""
        if self.independent_families is None or self.families_failed is None:
            return None
        bound = clopper_pearson_upper(self.families_failed, self.independent_families)
        return None if bound is None else round(bound, 4)


class CostOfLearning(BaseModel):
    """What validating controls cost the world it ran against.

    Validation re-runs the failing task and its siblings, and the refund
    environment moves money on every one of them. That spend is real and it
    belongs in the record instead of being treated as free because it happened
    against a fixture.
    """

    model_config = ConfigDict(extra="forbid")

    validation_runs: int = Field(ge=0)
    irreversible_actions: int = Field(ge=0)
    money_moved_usd: float = Field(ge=0)
    #: Re-runs whose directory was not retained, so their cost is not counted.
    runs_not_retained: list[str] = Field(default_factory=list)


class MetricsSnapshot(BaseModel):
    """One record per merge to main."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1.0", "0.2.0", "0.3.0"] = METRICS_SNAPSHOT_SCHEMA_VERSION
    commit: str = Field(min_length=1)
    recorded_at: datetime = Field(default_factory=utc_now)
    coverage: Coverage
    over_blocking: OverBlocking
    cost_of_learning: CostOfLearning
    suite_pass_rate: Ratio
    verified_failures: int = Field(ge=0)


def _excluded(path: Path, exclude: Sequence[Path]) -> bool:
    """Whether ``path`` sits under a directory the snapshot must not read.

    The collector writes its replay artifacts into the runs directory while the
    gate runs. Those are scratch, they are gitignored, and a snapshot that read
    them would record numbers from a throwaway validation and name a source
    file that stops existing the moment the directory is cleaned.
    """
    return any(path.is_relative_to(directory) for directory in exclude)


def _read_json(path: Path) -> dict | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def find_repair_validations(
    root: Path, *, exclude: Sequence[Path] = ()
) -> list[tuple[Path, RepairValidation]]:
    """Every readable ``repair_validation.json`` under ``root``, path-sorted.

    A malformed artifact is skipped rather than raising. The history writer
    runs after a merge has already happened, and one unreadable file must not
    cost the repository its record of that commit.
    """
    found = []
    for path in sorted(root.rglob(REPAIR_VALIDATION)):
        if _excluded(path, exclude):
            continue
        raw = _read_json(path)
        if raw is None:
            continue
        try:
            found.append((path, RepairValidation.model_validate(raw)))
        except Exception:
            continue
    return found


def prescribed_controls(root: Path, *, exclude: Sequence[Path] = ()) -> set[str]:
    """Control names every retained repair package asked for."""
    names: set[str] = set()
    for path in sorted(root.rglob(REPAIR_PACKAGE)):
        if _excluded(path, exclude):
            continue
        raw = _read_json(path)
        if raw is None:
            continue
        for control in raw.get("controls") or []:
            name = control.get("name") if isinstance(control, dict) else None
            if isinstance(name, str) and name:
                names.add(name)
    return names


def compute_coverage(prescribed: set[str], validations: list[RepairValidation]) -> Coverage:
    """Prescribed names narrowed to those that can exist and did survive."""
    materializable = {n for n in prescribed if MATERIALIZABLE_REPAIR_CONTROLS.get(n)}
    validated = {c.control for v in validations for c in v.controls} & prescribed
    verdicts = [
        c
        for v in validations
        for c in v.controls
        if c.verdict is ControlVerdict.ACCEPTED and c.control in prescribed
    ]
    accepted = {c.control for c in verdicts}
    gating = {c.control for c in verdicts if c.standing == "gating"}
    total = len(prescribed)
    return Coverage(
        prescribed=total,
        materializable=len(materializable),
        validated=len(validated),
        accepted=len(accepted),
        accepted_gating=len(gating),
        accepted_advisory=len(accepted - gating),
        accepted_over_prescribed=Ratio(numerator=len(accepted), denominator=total),
        materializable_over_prescribed=Ratio(numerator=len(materializable), denominator=total),
        unmapped_controls=sorted(prescribed - set(MATERIALIZABLE_REPAIR_CONTROLS)),
    )


def latest_validation(
    validations: list[tuple[Path, RepairValidation]],
) -> tuple[Path, RepairValidation] | None:
    """The most recent validation artifact, or None when there are none.

    Ordered by batch id, whose timestamp prefix sorts chronologically, with the
    path breaking ties so two artifacts from one batch order the same way on
    every checkout. File mtime would be the obvious choice and it is wrong here,
    because a fresh clone rewrites every mtime to the moment it was cloned.
    """
    if not validations:
        return None
    return max(validations, key=lambda pair: (pair[1].batch_id or "", str(pair[0])))


def compute_over_blocking(
    validation: tuple[Path, RepairValidation] | None, *, root: Path
) -> OverBlocking:
    """Sibling failures over sibling runs, for the latest validation artifact.

    One artifact rather than every artifact ever retained. Averaging the whole
    history into each point would flatten the trend this file exists to show,
    and a control rejected six months ago would keep dragging today's number
    down after the problem was fixed.
    """
    if validation is None:
        return OverBlocking(
            siblings_run=0,
            siblings_failed=0,
            rate=Ratio(numerator=0, denominator=0),
            independent_families=0,
            families_failed=0,
        )
    path, report = validation
    summary = over_blocking_summary(report.controls)
    return OverBlocking(
        siblings_run=summary.siblings_run,
        siblings_failed=summary.siblings_failed,
        rate=Ratio(numerator=summary.siblings_failed, denominator=summary.siblings_run),
        independent_families=summary.independent_families,
        families_failed=summary.families_failed,
        sources=[_relative(path, root)],
    )


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _run_dir(root: Path, run_id: str) -> Path | None:
    """Locate a re-run's directory under ``root``, whatever it is nested in."""
    direct = root / run_id
    if direct.is_dir():
        return direct
    for candidate in root.rglob(run_id):
        if candidate.is_dir():
            return candidate
    return None


def _irreversible_actions(run_dir: Path) -> int:
    """Successful tool calls in a run's trace that cannot be taken back."""
    trace = run_dir / TRACE
    if not trace.is_file():
        return 0
    count = 0
    for line in trace.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = event.get("payload") or {}
        if (
            event.get("event_type") == "tool_call_executed"
            and payload.get("side_effect") == IRREVERSIBLE_SIDE_EFFECT
            and payload.get("status") == "ok"
        ):
            count += 1
    return count


def _money_moved(run_dir: Path) -> float:
    """Refund dollars present in a run's final state."""
    state = _read_json(run_dir / FINAL_STATE)
    if state is None:
        return 0.0
    total = 0.0
    for refund in state.get("refunds") or []:
        amount = refund.get("amount_usd") if isinstance(refund, dict) else None
        if isinstance(amount, int | float):
            total += float(amount)
    return total


def compute_cost_of_learning(validation: RepairValidation | None, *, root: Path) -> CostOfLearning:
    """Irreversible actions and money moved by the latest validation's re-runs.

    Scoped to the same artifact over-blocking reads, so the two numbers in a
    record describe one validation session rather than two different ones.
    """
    if validation is None:
        return CostOfLearning(validation_runs=0, irreversible_actions=0, money_moved_usd=0.0)
    run_ids = []
    for control in validation.controls:
        reruns = [control.originating_rerun, *control.sibling_reruns]
        run_ids.extend(r.run_id for r in reruns if r is not None)
    # Deduplicated because two controls validated in one batch can name the
    # same sibling re-run, and that run's refund only happened once.
    unique = sorted(set(run_ids))

    actions = 0
    money = 0.0
    missing = []
    for run_id in unique:
        run_dir = _run_dir(root, run_id)
        if run_dir is None:
            missing.append(run_id)
            continue
        actions += _irreversible_actions(run_dir)
        money += _money_moved(run_dir)
    return CostOfLearning(
        validation_runs=len(unique) - len(missing),
        irreversible_actions=actions,
        money_moved_usd=round(money, 2),
        runs_not_retained=missing,
    )


def suite_pass_rate(root: Path, *, exclude: Sequence[Path] = ()) -> tuple[Ratio, int]:
    """Passed over completed, and the verified failure count, from batch summaries.

    Aggregated across every retained batch summary so the number covers the
    whole suite rather than whichever batch ran last.
    """
    passed = failed = 0
    for path in sorted(root.rglob("*batch_summary.json")):
        if _excluded(path, exclude):
            continue
        raw = _read_json(path)
        aggregates = (raw or {}).get("aggregates")
        if not isinstance(aggregates, dict):
            continue
        passed += int(aggregates.get("verifier_passed") or 0)
        failed += int(aggregates.get("verifier_failed") or 0)
    return Ratio(numerator=passed, denominator=passed + failed), failed


def build_snapshot(root: Path, *, commit: str, exclude: Sequence[Path] = ()) -> MetricsSnapshot:
    """Compute every measure from the artifacts retained under ``root``.

    ``exclude`` names directories whose artifacts are scratch, normally the
    runs directory the gate just wrote into. A snapshot describes what a commit
    retained, so it only reads files that commit actually carries.
    """
    exclude = [directory.resolve() for directory in exclude]
    root = root.resolve()
    pairs = find_repair_validations(root, exclude=exclude)
    validations = [v for _, v in pairs]
    latest = latest_validation(pairs)
    rate, failures = suite_pass_rate(root, exclude=exclude)
    return MetricsSnapshot(
        commit=commit,
        # Coverage reads every retained validation, because the question it
        # answers is what the library has accepted overall. The other two read
        # only the latest, because they are properties of one validation run.
        coverage=compute_coverage(prescribed_controls(root, exclude=exclude), validations),
        over_blocking=compute_over_blocking(latest, root=root),
        cost_of_learning=compute_cost_of_learning(latest[1] if latest else None, root=root),
        suite_pass_rate=rate,
        verified_failures=failures,
    )


def load_history(path: Path) -> list[MetricsSnapshot]:
    """Read the history file, skipping blank lines. Missing file means empty."""
    if not path.is_file():
        return []
    return [
        MetricsSnapshot.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def append_snapshot(path: Path, snapshot: MetricsSnapshot) -> bool:
    """Append one record, returning False when that commit is already recorded.

    The workflow can be re-run on a commit that already merged. Appending a
    second record for it would put two points on the same x with no way to
    tell which is current, so an existing commit is left alone.
    """
    if any(existing.commit == snapshot.commit for existing in load_history(path)):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    line = snapshot.model_dump_json() + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
    return True
