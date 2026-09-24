"""What an experiment is, so results can be compared instead of remembered (#155).

A batch is one run of one suite under one or more agent configurations. Nothing
in it says that two batches are the two sides of the same question. Run the
suite with a guardrail on and again with it off and you get two batch ids in the
batches folder, with the comparison living in somebody's notes.

An experiment is that missing record, in two files.

:class:`ExperimentSpec` is the plan, written *before* anything runs. It names
the hypothesis, freezes what must not change while the conditions run, and
lists the conditions themselves. Writing it first is the point: a plan composed
after seeing the numbers is not a plan.

:class:`ExperimentResult` is what came back. It maps each condition to the batch
that answered it, carries the metric set from the #27 memo, and records a
decision together with who or what made it.

There is deliberately no combined score. Eight named metrics, each with its own
derivation in ``docs/methodology_metrics.md``, and a human or a policy deciding
on the evidence rather than on an average of it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from trace_harness.environment.controls import resolve_control, select_controls
from trace_harness.runner.suite import AgentConfig
from trace_harness.tracing.events import utc_now

EXPERIMENT_SCHEMA_VERSION = "0.1.0"

#: An experiment id names a directory under ``runs/experiments/``, so it must be
#: a single path segment. Letters, digits, ``_`` and ``-`` only, which leaves no
#: separator, no dot and no way to climb out of that directory.
EXPERIMENT_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]*$"


def new_experiment_id() -> str:
    """``exp_YYYYMMDDTHHMMSSZ_xxxxxxxx``, matching the batch id style."""
    return f"exp_{utc_now():%Y%m%dT%H%M%SZ}_{uuid.uuid4().hex[:8]}"


class ConditionKind(StrEnum):
    """What a condition does to produce its runs.

    ``static_replay`` re-runs recorded actions and cannot react to being
    blocked, which is the limitation the whole replay-validity question exists
    to measure. The three live kinds differ only in what is installed or which
    model answers.
    """

    STATIC_REPLAY = "static_replay"
    LIVE = "live"
    LIVE_NO_CONTROL = "live_no_control"
    LIVE_SWAPPED = "live_swapped"


class Decision(StrEnum):
    BASELINE = "baseline"
    KEEP = "keep"
    DISCARD = "discard"
    REVIEW = "review"


class DecidedBy(StrEnum):
    HUMAN = "human"
    POLICY = "policy"


class StartPoint(BaseModel):
    """Where in a recorded run a condition begins, when not from the beginning."""

    model_config = ConfigDict(extra="forbid")

    source_run_id: str
    step_id: int = Field(ge=1)


class ConditionSpec(BaseModel):
    """One arm of the experiment."""

    model_config = ConfigDict(extra="forbid")

    name: str
    kind: ConditionKind
    agent_config: AgentConfig
    # Control ids installed for this arm. Validated when the plan loads, so a
    # typo fails before a sweep spends money.
    control_ids: list[str] = Field(default_factory=list)
    seeds: list[int] = Field(default_factory=list)
    start: StartPoint | None = None

    @field_validator("control_ids")
    @classmethod
    def _control_ids_are_installable(cls, control_ids: list[str]) -> list[str]:
        """Each id must name a control that can be installed.

        ``select_controls`` is the lookup ``replay --control`` uses and the
        branch stage (#159) installs through, and it raises for an unknown id.
        Each control it returns is then resolved through the guardrail
        registry, which raises for an unregistered ``guardrail_ref`` or a
        ``rule_ref`` its guardrail does not read. Every entry in the control
        library was committed from those same controls.
        """
        repeated = sorted({cid for cid in control_ids if control_ids.count(cid) > 1})
        if repeated:
            raise ValueError(f"control_ids lists {repeated} more than once")
        for control in select_controls(control_ids):
            resolve_control(control)
        return control_ids


class FrozenManifest(BaseModel):
    """What must not change while the conditions run.

    ``suite_id`` is enforced: ``experiment record`` refuses a batch whose
    summary names any other suite. ``fixtures_hash`` is stored exactly as the
    plan states it. Nothing here computes it from the fixture files or compares
    it with them, so it records what the author froze and proves nothing about
    the files. Computing it, and refusing a record when the files moved, is
    #195's ``experiment freeze``.
    """

    model_config = ConfigDict(extra="forbid")

    suite_id: str
    verifier_ids: list[str] = Field(default_factory=list)
    fixtures_hash: str


class Budget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_runs: int = Field(gt=0)
    max_cost_usd: float = Field(ge=0)


class ExperimentSpec(BaseModel):
    """The plan, written before anything runs."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = EXPERIMENT_SCHEMA_VERSION
    # Both defaults serve plans built in code. A plan read from a file must
    # state them, see ``load_plan``.
    experiment_id: str = Field(default_factory=new_experiment_id, pattern=EXPERIMENT_ID_PATTERN)
    brief_path: str | None = None
    hypothesis: str
    frozen_manifest: FrozenManifest
    conditions: list[ConditionSpec] = Field(min_length=1)
    budget: Budget
    created_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _condition_names_unique(self) -> ExperimentSpec:
        names = [c.name for c in self.conditions]
        if len(names) != len(set(names)):
            dupes = sorted({n for n in names if names.count(n) > 1})
            raise ValueError(f"conditions have duplicate name(s): {dupes}")
        return self


class ExperimentMetrics(BaseModel):
    """The eight metrics named in the #27 memo, and nothing else.

    Every field is optional because a condition set that never ran live cannot
    produce the divergence rates, and reporting a missing number as zero would
    be a lie. ``extra`` exists so a brief can carry something of its own
    without anybody quietly widening this set.

    The field names are asserted against the memo's appendix character for
    character in ``tests/test_experiment.py``.
    """

    model_config = ConfigDict(extra="forbid")

    verdict_agreement_rate: float | None = None
    first_post_fork_divergence_rate: float | None = None
    noise_floor_divergence_rate: float | None = None
    post_block_outcomes: dict[str, int] | None = None
    sibling_failure_rate: float | None = None
    verified_failure_count: int | None = None
    cost_usd: float | None = None
    latency_ms_p50: float | None = None

    extra: dict[str, float] = Field(default_factory=dict)

    @classmethod
    def memo_field_names(cls) -> list[str]:
        """The metric names, in declaration order, excluding ``extra``."""
        return [name for name in cls.model_fields if name != "extra"]


class ExperimentResult(BaseModel):
    """What came back, and what was decided on it."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = EXPERIMENT_SCHEMA_VERSION
    experiment_id: str = Field(pattern=EXPERIMENT_ID_PATTERN)
    # condition name -> batch id that answered it
    condition_batches: dict[str, str] = Field(default_factory=dict)
    metrics: ExperimentMetrics = Field(default_factory=ExperimentMetrics)
    decision: Decision
    decided_by: DecidedBy
    report_path: str | None = None
    finished_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


#: Fields a plan read from a file must state instead of taking a default.
PLAN_FILE_REQUIRED = ("experiment_id", "created_at")


def load_plan(data: Any) -> ExperimentSpec:
    """A plan read from a file, which must state its own id and creation time.

    The model defaults both so a plan can be built in code. Read from a file,
    a default would mint a fresh id on every read, so recording the same file
    twice would file two experiments, and the creation time would be the time
    of reading.
    """
    if isinstance(data, dict):
        missing = [name for name in PLAN_FILE_REQUIRED if name not in data]
        if missing:
            raise ValueError(f"an experiment plan file must state {', '.join(missing)}")
    return ExperimentSpec.model_validate(data)


class FrozenSuiteError(ValueError):
    """A recorded batch ran a different suite than the plan froze."""


def check_frozen_suite(spec: ExperimentSpec, suites: dict[str, str]) -> None:
    """Every recorded batch must have run the suite the plan froze.

    ``suites`` maps condition name to the ``suite_id`` in that condition's
    batch summary. A batch from another suite answered another question, so
    recording it would compare arms that never shared a task set.
    """
    frozen = spec.frozen_manifest.suite_id
    wrong = {name: suite for name, suite in sorted(suites.items()) if suite != frozen}
    if wrong:
        found = ", ".join(f"{name} ran {suite!r}" for name, suite in wrong.items())
        raise FrozenSuiteError(
            f"{spec.experiment_id} freezes suite {frozen!r}, but {found}; "
            "a batch from another suite cannot answer this plan"
        )


class UnknownConditionError(ValueError):
    """A recorded batch names a condition the spec never declared."""


def validate_condition_batches(spec: ExperimentSpec, condition_batches: dict[str, str]) -> None:
    """Every recorded condition must exist in the plan.

    Recording a batch under a name the spec never declared means the plan and
    the result describe different experiments, which is exactly the drift this
    contract exists to prevent.
    """
    declared = {c.name for c in spec.conditions}
    unknown = sorted(set(condition_batches) - declared)
    if unknown:
        raise UnknownConditionError(
            f"condition(s) {unknown} are not declared in {spec.experiment_id}; "
            f"declared conditions are {sorted(declared)}"
        )


def render_experiment_markdown(spec: ExperimentSpec, result: ExperimentResult) -> str:
    """A short human-readable report, written beside the two JSON files."""
    lines = [
        f"# {spec.experiment_id}",
        "",
        f"**Hypothesis.** {spec.hypothesis}",
        "",
        f"Suite `{spec.frozen_manifest.suite_id}`. Fixtures hash "
        f"`{spec.frozen_manifest.fixtures_hash}` as the plan states it; nothing "
        "recomputed it from the files. "
        f"Decision **{result.decision.value}** by {result.decided_by.value}.",
        "",
        "## Conditions",
        "",
        "| condition | kind | controls | batch |",
        "|---|---|---|---|",
    ]
    for condition in spec.conditions:
        controls = ", ".join(condition.control_ids) or "none"
        batch = result.condition_batches.get(condition.name, "not recorded")
        lines.append(f"| {condition.name} | {condition.kind.value} | {controls} | {batch} |")

    lines += ["", "## Metrics", "", "| metric | value |", "|---|---|"]
    for name in ExperimentMetrics.memo_field_names():
        value = getattr(result.metrics, name)
        lines.append(f"| {name} | {'not measured' if value is None else value} |")
    for name, value in sorted(result.metrics.extra.items()):
        lines.append(f"| {name} (extra) | {value} |")
    return "\n".join(lines) + "\n"


def derive_metrics(
    batch_summaries: list[Any], *, condition_names: dict[str, str] | None = None
) -> ExperimentMetrics:
    """Compute every metric the batch summaries can support today.

    Three of the eight come from a batch alone, each as Part B2 of
    ``docs/methodology_metrics.md`` defines it:

    - ``verified_failure_count`` counts completed runs whose verdict is
      ``fail``. It is ``None`` when no completed run carries a verdict, since
      nothing was verified. The batch entry does not record
      ``blocks_release``, so a failure of a non-blocking check counts too.
    - ``cost_usd`` sums the costs that were recorded, with the number of runs
      that recorded one and the number of runs in ``extra`` as
      ``cost_recorded_k`` and ``cost_recorded_n``.
    - ``latency_ms_p50`` is the median over completed runs.

    All three pool every recorded condition. ``condition_names`` maps a batch
    id to the condition it answered, and when more than one condition is
    recorded the failure count and the median are also given per condition in
    ``extra``, as ``verified_failure_count.<condition>`` and
    ``latency_ms_p50.<condition>``. A fixture arm's near-zero latency would
    otherwise disappear into the pooled median.

    The divergence rates and post-block outcomes need the branch stage (#159)
    and the post-block classifier (#157) to have produced their fields, and
    ``verdict_agreement_rate`` and ``sibling_failure_rate`` need a live arm to
    compare against. Those stay ``None``, because a zero there would read as a
    measurement.
    """
    from trace_harness.runner.batch import BatchSummary

    summaries = [
        s if isinstance(s, BatchSummary) else BatchSummary.model_validate(s)
        for s in batch_summaries
    ]
    entries = [e for s in summaries for e in s.entries]
    extra: dict[str, float] = {}

    costs = [e.cost_usd for e in entries if e.cost_usd is not None]
    if entries:
        extra["cost_recorded_k"] = len(costs)
        extra["cost_recorded_n"] = len(entries)

    names = condition_names or {}
    by_condition: dict[str, list[Any]] = {}
    for summary in summaries:
        name = names.get(summary.batch_id, summary.batch_id)
        by_condition.setdefault(name, []).extend(summary.entries)
    if len(by_condition) > 1:
        for name, condition_entries in sorted(by_condition.items()):
            failures = _verified_failures(condition_entries)
            if failures is not None:
                extra[f"verified_failure_count.{name}"] = failures
            latency = _completed_latency_p50(condition_entries)
            if latency is not None:
                extra[f"latency_ms_p50.{name}"] = latency

    return ExperimentMetrics(
        verified_failure_count=_verified_failures(entries),
        cost_usd=round(sum(costs), 6) if costs else None,
        latency_ms_p50=_completed_latency_p50(entries),
        extra=extra,
    )


def _verified_failures(entries: list[Any]) -> int | None:
    """Completed runs with verdict ``fail``, or None when none was verified."""
    verified = [e for e in entries if e.status == "completed" and e.verdict in ("pass", "fail")]
    if not verified:
        return None
    return sum(1 for e in verified if e.verdict == "fail")


def _completed_latency_p50(entries: list[Any]) -> float | None:
    completed = [e for e in entries if e.status == "completed" and e.latency_ms is not None]
    return _median(sorted(e.latency_ms for e in completed))


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2
