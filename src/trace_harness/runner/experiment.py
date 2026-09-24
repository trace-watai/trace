"""What an experiment is, so results can be compared instead of remembered (#155).

A batch is one run of one suite under one or more agent configurations. Nothing
in it says that two batches are the two sides of the same question. Running the
suite with a guardrail on and again with it off leaves two batch ids in the
batches folder, with the comparison living in somebody's notes.

An experiment is that missing record, in two files.

:class:`ExperimentSpec` is the plan, written *before* anything runs. It names
the hypothesis, freezes what must not change while the conditions run, and
lists the conditions themselves. Writing it first is the point, since a plan
composed after seeing the numbers can be fitted to them.

:class:`ExperimentResult` is what came back. It maps each condition to the batch
that answered it, carries the metric set from the #27 memo, and records a
decision together with who or what made it.

There is deliberately no combined score. Eight named metrics, each with its own
derivation in ``docs/methodology_metrics.md``, and a human or a policy deciding
on that evidence itself. An average of it would decide nothing.
"""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from trace_harness.environment.controls import resolve_control, select_controls
from trace_harness.runner.frozen_set import FrozenComponent, FrozenFileChange, check_labels_path
from trace_harness.runner.suite import AgentConfig
from trace_harness.tracing.events import utc_now

# 0.3.0: ConditionSpec.continuation_script (#159); 0.2.0: the frozen set on the
# plan and the frozen_set_* fields on the result (#195)
EXPERIMENT_SCHEMA_VERSION = "0.3.0"
# Plans at this version predate the frozen set and may record without one.
PRE_FROZEN_SET_SCHEMA_VERSION = "0.1.0"

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
    summary names any other suite.

    ``frozen_set`` covers the fixtures, the verifier, the environment, the
    attribution scorer, the suite and the labels (#195). ``experiment freeze``
    writes it and sets ``fixtures_hash`` to its fixtures digest, and
    ``experiment record`` recomputes it and refuses when a file moved. If the
    fixtures move between two conditions, the conditions answered different
    questions and the comparison is void.

    Without a frozen set, ``fixtures_hash`` is stored exactly as the plan
    states it. Nothing computes it from the fixture files or compares it with
    them, so it records what the author froze and proves nothing about the
    files. Only a plan from schema 0.1.0 records without a frozen set.
    """

    model_config = ConfigDict(extra="forbid")

    suite_id: str
    verifier_ids: list[str] = Field(default_factory=list)
    fixtures_hash: str
    labels_path: str | None = None
    frozen_set: dict[str, FrozenComponent] | None = None

    @model_validator(mode="after")
    def _labels_path_stays_in_the_repository(self) -> FrozenManifest:
        if self.labels_path is not None:
            check_labels_path(self.labels_path)
        return self

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

    # A count such as the k or n behind a rate stays an integer on disk.
    # Results written with floats before still load.
    extra: dict[str, int | float] = Field(default_factory=dict)

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
    # How the plan's frozen set compared with the tree at record time (#195).
    # Both false means the plan carried no frozen set, which is never a pass.
    frozen_set_verified: bool = False
    frozen_set_drifted: bool = False
    frozen_set_drift: list[FrozenFileChange] = Field(default_factory=list)

    @model_validator(mode="after")
    def _drift_forces_review(self) -> ExperimentResult:
        """A result that says drifted carries its files and decision review.

        This refuses an edit to the decision alone. An edit that also clears
        the drift fields describes a clean record and loads; the file carries
        no signature, so git history is the record against that.
        """
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


class MixedLiveModelsError(ValueError):
    """The batches behind the live metrics ran more than one real model."""


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
        _suite_line(spec, result)
        + f"Decision **{result.decision.value}** by {result.decided_by.value}.",
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
    if result.frozen_set_drift:
        lines += ["", "## Frozen set drift", "", "| component | change | file |", "|---|---|---|"]
        for c in result.frozen_set_drift:
            lines.append(f"| {c.component} | {c.change} | {c.path} |")
    return "\n".join(lines) + "\n"


def _suite_line(spec: ExperimentSpec, result: ExperimentResult) -> str:
    """Say "frozen at" only when record time checked the frozen set and it held."""
    manifest = spec.frozen_manifest
    if result.frozen_set_verified:
        return f"Suite `{manifest.suite_id}` frozen at `{manifest.fixtures_hash}`. "
    if manifest.frozen_set is not None:
        return f"Suite `{manifest.suite_id}`. Fixtures hash `{manifest.fixtures_hash}` at freeze. "
    return (
        f"Suite `{manifest.suite_id}`. Fixtures hash `{manifest.fixtures_hash}` as the plan "
        "states it; nothing recomputed it from the files. "
    )


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
    batch_summaries: list[Any], *, conditions: dict[str, ConditionSpec] | None = None
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

    All three pool every recorded condition. ``conditions`` maps a batch id to
    the plan's condition it answered, and when more than one condition is
    recorded the failure count and the median are also given per condition in
    ``extra``, as ``verified_failure_count.<condition>`` and
    ``latency_ms_p50.<condition>``. A fixture arm's near-zero latency would
    otherwise disappear into the pooled median.

    With ``conditions``, the branch stage's entry fields (#159) also give the
    two divergence rates, over ``live`` and ``live_no_control`` batches, and
    the post-block outcome counts over ``live`` batches, as Part B2 defines
    them. ``live_swapped`` batches feed none of the three, because the
    pre-registration reports each live model on its own. The counts behind
    each rate go in ``extra`` so the rate is never read without its
    denominator. Nothing derives ``verdict_agreement_rate`` or
    ``sibling_failure_rate`` yet, so they stay ``None``, since a zero there
    would read as a measurement.

    The three read one model. :class:`MixedLiveModelsError` is raised when the
    ``live`` and ``live_no_control`` batches ran more than one real provider
    and model, since a rate and its noise floor from different agents compare
    nothing. Fixture batches, such as the harness check, are left out of the
    three when a real model's batches are recorded beside them, and
    ``extra["live_fixture_batches_excluded"]`` counts them. With no real model
    recorded, fixture batches feed the three.
    """
    from trace_harness.runner.batch import BatchSummary

    summaries = [
        s if isinstance(s, BatchSummary) else BatchSummary.model_validate(s)
        for s in batch_summaries
    ]
    entries = [e for s in summaries for e in s.entries]
    answered = conditions or {}
    kinds = {batch_id: condition.kind for batch_id, condition in answered.items()}
    extra: dict[str, int | float] = {}
    excluded = _fixture_batches_beside_a_real_model(summaries, kinds)
    if excluded:
        extra["live_fixture_batches_excluded"] = len(excluded)

    def of_kind(kind: ConditionKind) -> list[Any]:
        return [
            e
            for s in summaries
            if kinds.get(s.batch_id) is kind and s.batch_id not in excluded
            for e in s.entries
        ]

    costs = [e.cost_usd for e in entries if e.cost_usd is not None]
    if entries:
        extra["cost_recorded_k"] = len(costs)
        extra["cost_recorded_n"] = len(entries)

    by_condition: dict[str, list[Any]] = {}
    for summary in summaries:
        condition = answered.get(summary.batch_id)
        name = condition.name if condition is not None else summary.batch_id
        by_condition.setdefault(name, []).extend(summary.entries)
    if len(by_condition) > 1:
        for name, condition_entries in sorted(by_condition.items()):
            failures = _verified_failures(condition_entries)
            if failures is not None:
                extra[f"verified_failure_count.{name}"] = failures
            latency = _completed_latency_p50(condition_entries)
            if latency is not None:
                extra[f"latency_ms_p50.{name}"] = latency

    control_on = of_kind(ConditionKind.LIVE)
    outcomes = Counter(str(e.post_block_outcome) for e in control_on if e.post_block_outcome)

    return ExperimentMetrics(
        first_post_fork_divergence_rate=_divergence_rate(
            control_on, extra, "first_post_fork_divergence"
        ),
        noise_floor_divergence_rate=_divergence_rate(
            of_kind(ConditionKind.LIVE_NO_CONTROL), extra, "noise_floor_divergence"
        ),
        post_block_outcomes=dict(sorted(outcomes.items())) or None,
        verified_failure_count=_verified_failures(entries),
        cost_usd=round(sum(costs), 6) if costs else None,
        latency_ms_p50=_completed_latency_p50(entries),
        extra=extra,
    )


#: The condition kinds whose batches feed the divergence rates and outcome counts.
_ARM_KINDS = (ConditionKind.LIVE, ConditionKind.LIVE_NO_CONTROL)
_FIXTURE = ("fixture", None)


def _batch_models(summary: Any) -> set[tuple[str, str | None]]:
    """The provider and model pairs a batch ran, each default model resolved.

    Fixture runs count as one pair whatever script played them.
    """
    from trace_harness.models import resolve_model_name

    models: set[tuple[str, str | None]] = set()
    for config in summary.agent_configs:
        if config.provider == _FIXTURE[0]:
            models.add(_FIXTURE)
            continue
        try:
            models.add((config.provider, resolve_model_name(config.provider, config.model, None)))
        except ValueError:
            models.add((config.provider, config.model))
    return models


def _fixture_batches_beside_a_real_model(
    summaries: list[Any], kinds: dict[str, ConditionKind]
) -> set[str]:
    """The fixture batch ids to leave out of the live metrics, after refusing a mix."""
    arms = [s for s in summaries if kinds.get(s.batch_id) in _ARM_KINDS]
    real = [s for s in arms if _batch_models(s) != {_FIXTURE}]
    models = sorted({m for s in real for m in _batch_models(s)}, key=lambda m: (m[0], m[1] or ""))
    if len(models) > 1:
        named = ", ".join(p if m is None else f"{p} {m}" for p, m in models)
        raise MixedLiveModelsError(
            f"the live and live_no_control batches ran more than one model ({named}), so "
            "their divergence rates would compare different agents; record one model's "
            "batches for those conditions"
        )
    if not real:
        return set()
    return {s.batch_id for s in arms} - {s.batch_id for s in real}


def _divergence_rate(entries: list[Any], extra: dict[str, int | float], name: str) -> float | None:
    """``diverged / completed`` over completed runs.

    A completed run that took no action after the fork has nothing to compare
    and is left out. ``branch`` refuses the conditions that would produce one,
    so in its batches the denominator is every completed run.
    """
    compared = [e for e in entries if e.status == "completed" and e.diverged is not None]
    if not compared:
        return None
    diverged = sum(1 for e in compared if e.diverged)
    extra[f"{name}_k"] = diverged
    extra[f"{name}_n"] = len(compared)
    return round(diverged / len(compared), 4)


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
