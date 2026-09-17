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
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from trace_harness.runner.suite import AgentConfig
from trace_harness.tracing.events import utc_now

EXPERIMENT_SCHEMA_VERSION = "0.1.0"


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
    # Control ids installed for this arm. Validated against the registry at
    # spec load time so a typo fails before a sweep spends money.
    control_ids: list[str] = Field(default_factory=list)
    seeds: list[int] = Field(default_factory=list)
    start: StartPoint | None = None


class FrozenManifest(BaseModel):
    """What must not change while the conditions run.

    ``fixtures_hash`` is what makes the freeze checkable rather than asserted.
    If the fixtures move between two conditions, the conditions answered
    different questions and the comparison is void.
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
    experiment_id: str = Field(default_factory=new_experiment_id)
    brief_path: str | None = None
    hypothesis: str
    frozen_manifest: FrozenManifest
    conditions: list[ConditionSpec] = Field(min_length=1)
    budget: Budget
    created_at: Any = Field(default_factory=utc_now)
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
    experiment_id: str
    # condition name -> batch id that answered it
    condition_batches: dict[str, str] = Field(default_factory=dict)
    metrics: ExperimentMetrics = Field(default_factory=ExperimentMetrics)
    decision: Decision
    decided_by: DecidedBy
    report_path: str | None = None
    finished_at: Any = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


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
        f"Suite `{spec.frozen_manifest.suite_id}` frozen at "
        f"`{spec.frozen_manifest.fixtures_hash}`. "
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


def derive_metrics(batch_summaries: list[Any]) -> ExperimentMetrics:
    """Compute every metric the batch summaries can support today.

    Only four of the eight are derivable from a batch alone. The divergence
    rates and post-block outcomes need the branch stage (#159) and the
    post-block classifier (#157) to have produced their fields, and
    ``verdict_agreement_rate`` needs a live arm to disagree with. Those stay
    ``None`` rather than being filled with a placeholder, because a zero here
    would read as a measurement.
    """
    from trace_harness.runner.batch import BatchSummary

    summaries = [
        s if isinstance(s, BatchSummary) else BatchSummary.model_validate(s)
        for s in batch_summaries
    ]
    entries = [e for s in summaries for e in s.entries]

    verified_failures = sum(1 for e in entries if e.verdict == "fail")
    costs = [e.cost_usd for e in entries if e.cost_usd is not None]
    latencies = sorted(e.latency_ms for e in entries if e.latency_ms is not None)

    return ExperimentMetrics(
        verified_failure_count=verified_failures,
        cost_usd=round(sum(costs), 6) if costs else None,
        latency_ms_p50=_median(latencies),
    )


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2
