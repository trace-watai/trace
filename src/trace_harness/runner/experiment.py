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
from collections import Counter
from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from trace_harness.runner.frozen_set import FrozenComponent, FrozenFileChange
from trace_harness.runner.suite import AgentConfig
from trace_harness.tracing.events import utc_now

# 0.3.0: ConditionSpec.continuation_script (#159); 0.2.0: the frozen set on the
# plan and the frozen_set_* fields on the result (#195)
EXPERIMENT_SCHEMA_VERSION = "0.3.0"
# Plans at this version predate the frozen set and may record without one.
PRE_FROZEN_SET_SCHEMA_VERSION = "0.1.0"


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
    # A fixture script whose actions are played after the start step, for
    # offline tests of the branch stage (#159). Absent means the recorded
    # continuation. Only the fixture provider plays scripts.
    continuation_script: str | None = None

    @model_validator(mode="after")
    def _script_needs_fixture_provider(self) -> ConditionSpec:
        if self.continuation_script and self.agent_config.provider != "fixture":
            raise ValueError(
                f"condition {self.name!r}: continuation_script needs provider 'fixture', "
                f"got {self.agent_config.provider!r}"
            )
        return self


class FrozenManifest(BaseModel):
    """What must not change while the conditions run.

    ``fixtures_hash`` is what makes the freeze checkable rather than asserted.
    If the fixtures move between two conditions, the conditions answered
    different questions and the comparison is void.

    ``frozen_set`` extends that to the verifier, the environment, the
    attribution scorer, the suite and the labels (#195). ``experiment freeze``
    writes it and sets ``fixtures_hash`` to its fixtures digest. It is None
    only on plans from schema 0.1.0, whose ``fixtures_hash`` was entered by
    hand and is checked by nothing.
    """

    model_config = ConfigDict(extra="forbid")

    suite_id: str
    verifier_ids: list[str] = Field(default_factory=list)
    fixtures_hash: str
    labels_path: str | None = None
    frozen_set: dict[str, FrozenComponent] | None = None

    @model_validator(mode="after")
    def _fixtures_hash_is_the_frozen_digest(self) -> FrozenManifest:
        fixtures = (self.frozen_set or {}).get("fixtures")
        if fixtures is not None and fixtures.digest != self.fixtures_hash:
            raise ValueError(
                f"fixtures_hash {self.fixtures_hash} disagrees with the frozen fixtures "
                f"digest {fixtures.digest}"
            )
        return self


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
    # How the plan's frozen set compared with the tree at record time (#195).
    # Both false means the plan carried no frozen set, which is never a pass.
    frozen_set_verified: bool = False
    frozen_set_drifted: bool = False
    frozen_set_drift: list[FrozenFileChange] = Field(default_factory=list)

    @model_validator(mode="after")
    def _drift_forces_review(self) -> ExperimentResult:
        if self.frozen_set_verified and self.frozen_set_drifted:
            raise ValueError("a frozen set cannot be both verified and drifted")
        if self.frozen_set_drifted != bool(self.frozen_set_drift):
            raise ValueError("frozen_set_drifted must be true exactly when frozen_set_drift is set")
        if self.frozen_set_drifted and self.decision is not Decision.REVIEW:
            raise ValueError(
                "a result recorded over a drifted frozen set must have decision review, "
                f"got {self.decision.value}"
            )
        return self


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
        _frozen_set_line(result),
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
    lines += _pair_lines(result.metadata.get("verdict_agreement_pairs") or [])
    if result.frozen_set_drift:
        lines += ["", "## Frozen set drift", "", "| component | change | file |", "|---|---|---|"]
        for c in result.frozen_set_drift:
            lines.append(f"| {c.component} | {c.change} | {c.path} |")
    return "\n".join(lines) + "\n"


def _pair_lines(pairs: list[dict[str, Any]]) -> list[str]:
    """The verdict agreement pairs, each excluded pair with its reason."""
    if not pairs:
        return []
    lines = [
        "",
        "## Verdict agreement pairs",
        "",
        "| arm | model | task | control | static clear | clear seeds | agrees |",
        "|---|---|---|---|---|---|---|",
    ]
    for p in pairs:
        agrees = f"excluded: {p['excluded']}" if p["excluded"] else p["agrees"]
        lines.append(
            f"| {p['kind']} | {p['model']} | {p['task_id']} | {p['control']} "
            f"| {p['static_clear']} | {p['clear_seeds']}/{p['completed_seeds']} | {agrees} |"
        )
    return lines


def _frozen_set_line(result: ExperimentResult) -> str:
    if result.frozen_set_drifted:
        return (
            f"**Frozen set drifted.** {len(result.frozen_set_drift)} file(s) differed from "
            "the plan at record time, so the decision is forced to review."
        )
    if result.frozen_set_verified:
        return "Frozen set verified: every frozen file matched the plan at record time."
    return "Frozen set not recorded: the plan predates it, so the evaluator was not checked."


def derive_metrics(
    batch_summaries: list[Any],
    condition_kinds: dict[str, ConditionKind] | None = None,
    *,
    conditions: dict[str, ConditionSpec] | None = None,
    verifier_results: Mapping[str, Any] | None = None,
) -> ExperimentMetrics:
    """Compute every metric the batch summaries can support.

    ``condition_kinds`` maps a batch id to the kind of the condition it
    answered. With it, the branch stage's entry fields (#159) give the two
    divergence rates, over ``live`` and ``live_no_control`` batches, and the
    post-block outcome counts over ``live`` batches, as Part B2 of
    docs/methodology_metrics.md defines them. ``live_swapped`` batches feed
    none of the three, because the pre-registration reports each live model on
    its own. The counts behind each rate go in ``extra`` so the rate is never
    read without its denominator.

    ``conditions`` maps a batch id to the plan's condition, and
    ``verifier_results`` maps a run id to its verifier result. With both,
    ``verdict_agreement_rate`` and ``sibling_failure_rate`` are derived as
    ``runner/verdict_agreement.py`` documents (#200). Without them the two
    stay ``None``, since a zero there would read as a measurement.
    """
    from trace_harness.runner.batch import BatchSummary
    from trace_harness.runner.verdict_agreement import (
        agreement_rate,
        recorded_batches,
        score_pairs,
        sibling_failures,
    )

    summaries = [
        s if isinstance(s, BatchSummary) else BatchSummary.model_validate(s)
        for s in batch_summaries
    ]
    entries = [e for s in summaries for e in s.entries]
    kinds = condition_kinds or {b: c.kind for b, c in (conditions or {}).items()}

    def of_kind(kind: ConditionKind) -> list[Any]:
        return [e for s in summaries if kinds.get(s.batch_id) is kind for e in s.entries]

    verified_failures = sum(1 for e in entries if e.verdict == "fail")
    costs = [e.cost_usd for e in entries if e.cost_usd is not None]
    latencies = sorted(e.latency_ms for e in entries if e.latency_ms is not None)
    extra: dict[str, float] = {}
    control_on = of_kind(ConditionKind.LIVE)
    outcomes = Counter(str(e.post_block_outcome) for e in control_on if e.post_block_outcome)

    agreement = siblings = None
    if conditions is not None and verifier_results is not None:
        recorded = recorded_batches(summaries, conditions)
        agreement = agreement_rate(score_pairs(recorded, verifier_results), extra)
        failed_siblings = sibling_failures(recorded, verifier_results)
        if failed_siblings is not None:
            k, n = failed_siblings
            extra["sibling_failure_k"], extra["sibling_failure_n"] = k, n
            siblings = round(k / n, 4)

    return ExperimentMetrics(
        verdict_agreement_rate=agreement,
        sibling_failure_rate=siblings,
        first_post_fork_divergence_rate=_divergence_rate(
            control_on, extra, "first_post_fork_divergence"
        ),
        noise_floor_divergence_rate=_divergence_rate(
            of_kind(ConditionKind.LIVE_NO_CONTROL), extra, "noise_floor_divergence"
        ),
        post_block_outcomes=dict(sorted(outcomes.items())) or None,
        verified_failure_count=verified_failures,
        cost_usd=round(sum(costs), 6) if costs else None,
        latency_ms_p50=_median(latencies),
        extra=extra,
    )


def _divergence_rate(entries: list[Any], extra: dict[str, float], name: str) -> float | None:
    """``diverged / completed`` over the completed runs the branch stage compared."""
    compared = [e for e in entries if e.status == "completed" and e.diverged is not None]
    if not compared:
        return None
    diverged = sum(1 for e in compared if e.diverged)
    extra[f"{name}_k"] = diverged
    extra[f"{name}_n"] = len(compared)
    return round(diverged / len(compared), 4)


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2
