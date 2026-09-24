"""Verdict agreement and sibling failures, the two B2 metrics a batch cannot give alone (#200).

``verdict_agreement_rate`` pairs the static replay verdict on an artifact and
control with the majority live verdict over the same artifact, control and
model. ``sibling_failure_rate`` is A4 over the experiment's own replays. Both
read per-run verifier results, because "a blocking failure after the fork" is
a fact about a check's ``step_ids`` and a batch entry keeps only the verdict.

Pre-registration 001 fixes the rules followed here.

- The static verdict is clear when ``replay --apply-control`` exited 0, which
  the branch stage records as ``replay_exit_code`` on the ``static_replay``
  batch of one.
- A live seed is clear when its completed run records no blocking failure
  after the fork. The live verdict is clear when at least half of the pair's
  completed control-on seeds are clear.
- A pair with fewer than :data:`MIN_COMPLETED_SEEDS` completed seeds is
  insufficient and left out of the rate, with the exclusion stated.
- Each live model's rate is computed separately. The headline rate is the
  ``live`` arm's, and only when one model answers that arm.

The per-seed share behind every majority goes in ``metrics.extra``, since the
memo's blind spot for this metric is that a majority hides bimodal seeds.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from trace_harness.runner.batch import BatchRunEntry, BatchSummary
from trace_harness.runner.experiment import ConditionKind, ConditionSpec
from trace_harness.verifiers.base import VerifierResult, VerifierVerdict

#: Pre-registration 001 leaves a pair with fewer completed seeds out of the rate.
MIN_COMPLETED_SEEDS = 5
#: Every fixture continuation counts as one model, whatever script it plays.
FIXTURE_MODEL = "fixture"
#: Arms whose runs have the control installed and so give a live verdict.
CONTROL_ON_KINDS = (ConditionKind.LIVE, ConditionKind.LIVE_SWAPPED)


@dataclass(frozen=True)
class RecordedBatch:
    """One recorded batch and the plan's condition it answered."""

    summary: BatchSummary
    condition: ConditionSpec

    @property
    def kind(self) -> ConditionKind:
        return self.condition.kind

    @property
    def source_run_id(self) -> str | None:
        """The artifact that ran, as its pinned source run, which is unique per artifact."""
        start = self.condition.start
        return self.summary.metadata.get("source_run_id") or (
            start.source_run_id if start else None
        )

    @property
    def fork_step(self) -> int:
        start = self.condition.start
        return start.step_id if start and self.kind is not ConditionKind.STATIC_REPLAY else 0

    @property
    def control(self) -> str:
        return "+".join(sorted(self.condition.control_ids))


@dataclass(frozen=True)
class PairVerdict:
    """The static and live verdicts of one artifact, control and model under one arm."""

    kind: str
    model: str
    artifact: str
    task_id: str
    control: str
    fork_step: int
    static_clear: bool | None
    clear_seeds: int
    completed_seeds: int
    excluded: str | None

    @property
    def live_clear(self) -> bool:
        return self.completed_seeds > 0 and 2 * self.clear_seeds >= self.completed_seeds

    @property
    def agrees(self) -> bool | None:
        return None if self.excluded else self.static_clear == self.live_clear


def recorded_batches(
    summaries: list[BatchSummary], conditions: Mapping[str, ConditionSpec]
) -> list[RecordedBatch]:
    """Pair each summary with its condition, by batch id; unmapped batches are left out."""
    return [RecordedBatch(s, conditions[s.batch_id]) for s in summaries if s.batch_id in conditions]


def model_key(entry: BatchRunEntry) -> str:
    if entry.provider == FIXTURE_MODEL:
        return FIXTURE_MODEL
    return entry.model or entry.provider


def blocking_after_fork(verdict: VerifierResult, fork_step: int) -> bool:
    """A failed verdict with a release-blocking check at a step after the fork.

    This is B1's "blocking failure after the fork" and the live half of the
    agreement rule. A check with no step ids never falls after the fork.
    """
    return verdict.verdict is VerifierVerdict.FAIL and any(
        check.blocks_release and any(step > fork_step for step in check.step_ids)
        for check in verdict.failed_checks
    )


def judged(entry: BatchRunEntry, verdicts: Mapping[str, VerifierResult]) -> VerifierResult | None:
    """The verdict of a completed run, or None for a run no denominator counts."""
    if entry.status != "completed" or entry.run_id is None:
        return None
    verdict = verdicts.get(entry.run_id)
    if verdict is None or verdict.verdict is VerifierVerdict.INCOMPLETE:
        return None
    return verdict


def score_pairs(
    batches: list[RecordedBatch], verdicts: Mapping[str, VerifierResult]
) -> list[PairVerdict]:
    """Every (arm, artifact, control, model) with its static and live verdicts."""
    static: dict[tuple[str | None, str], bool] = {}
    for batch in batches:
        code = batch.summary.metadata.get("replay_exit_code")
        if batch.kind is ConditionKind.STATIC_REPLAY and batch.control and code is not None:
            static[(batch.source_run_id, batch.control)] = code == 0

    groups: dict[tuple[str, str, str, str], list[tuple[BatchRunEntry, int]]] = defaultdict(list)
    fork_steps: dict[tuple[str, str, str, str], int] = {}
    for batch in batches:
        if batch.kind not in CONTROL_ON_KINDS or not batch.control or not batch.source_run_id:
            continue
        for entry in batch.summary.entries:
            key = (batch.kind.value, batch.source_run_id, batch.control, model_key(entry))
            groups[key].append((entry, batch.fork_step))
            fork_steps[key] = batch.fork_step

    pairs = []
    for key, runs in sorted(groups.items()):
        kind, artifact, control, model = key
        seeds = [(verdict, fork) for entry, fork in runs if (verdict := judged(entry, verdicts))]
        clear = sum(1 for verdict, fork in seeds if not blocking_after_fork(verdict, fork))
        static_clear = static.get((artifact, control))
        excluded = None
        if len(seeds) < MIN_COMPLETED_SEEDS:
            excluded = f"{len(seeds)} completed seed(s), fewer than {MIN_COMPLETED_SEEDS}"
        elif static_clear is None:
            excluded = "no static_replay verdict for this artifact and control"
        pairs.append(
            PairVerdict(
                kind=kind,
                model=model,
                artifact=artifact,
                task_id=runs[0][0].task_id,
                control=control,
                fork_step=fork_steps[key],
                static_clear=static_clear,
                clear_seeds=clear,
                completed_seeds=len(seeds),
                excluded=excluded,
            )
        )
    return pairs


def agreement_rate(pairs: list[PairVerdict], extra: dict[str, float]) -> float | None:
    """Fill the per-model rates and per-pair shares into ``extra``; return the headline.

    The headline is the ``live`` arm's rate over its sufficient pairs, and None
    when that arm has no sufficient pair or is answered by more than one model,
    since the pre-registration never pools models.
    """
    labels = _artifact_labels(pairs)
    for pair in pairs:
        prefix = f"pair/{pair.kind}/{pair.model}/{labels[pair.artifact]}/{pair.control}"
        extra[f"{prefix}/completed_seeds"] = pair.completed_seeds
        if pair.completed_seeds:
            extra[f"{prefix}/live_clear_share"] = round(pair.clear_seeds / pair.completed_seeds, 4)
        if pair.static_clear is not None:
            extra[f"{prefix}/static_clear"] = float(pair.static_clear)

    rates: dict[tuple[str, str], float | None] = {}
    for kind, model in sorted({(p.kind, p.model) for p in pairs}):
        rates[(kind, model)] = _rate(
            [p for p in pairs if (p.kind, p.model) == (kind, model)],
            extra,
            f"/{kind}/{model}",
        )
    live_models = sorted({p.model for p in pairs if p.kind == ConditionKind.LIVE.value})
    if len(live_models) != 1:
        return None
    (model,) = live_models
    return _rate([p for p in pairs if (p.kind, p.model) == ("live", model)], extra, "")


def _rate(pairs: list[PairVerdict], extra: dict[str, float], suffix: str) -> float | None:
    scored = [p for p in pairs if p.agrees is not None]
    extra[f"verdict_agreement_excluded{suffix}"] = len(pairs) - len(scored)
    if not scored:
        return None
    agreeing = sum(1 for p in scored if p.agrees)
    extra[f"verdict_agreement_k{suffix}"] = agreeing
    extra[f"verdict_agreement_n{suffix}"] = len(scored)
    rate = round(agreeing / len(scored), 4)
    if suffix:
        extra[f"verdict_agreement_rate{suffix}"] = rate
    return rate


def _artifact_labels(pairs: list[PairVerdict]) -> dict[str, str]:
    """The task id names an artifact unless two artifacts share one."""
    tasks: dict[str, set[str]] = defaultdict(set)
    for pair in pairs:
        tasks[pair.task_id].add(pair.artifact)
    return {
        p.artifact: p.task_id if len(tasks[p.task_id]) == 1 else f"{p.task_id}@{p.artifact}"
        for p in pairs
    }


def sibling_failures(
    batches: list[RecordedBatch], verdicts: Mapping[str, VerifierResult]
) -> tuple[int, int] | None:
    """A4 over the experiment's own replays: failed siblings over judged siblings.

    Siblings come from each ``static_replay`` batch that installed a control,
    as the replay ran them, so a sibling shared by two artifacts counts once
    per replay. A sibling counts as failed when its verdict is ``fail``, and a
    sibling whose run never completed stays out of the denominator.
    """
    judged_siblings = []
    for batch in batches:
        if batch.kind is not ConditionKind.STATIC_REPLAY or not batch.control:
            continue
        for sibling in batch.summary.metadata.get("siblings") or []:
            verdict = verdicts.get(sibling.get("run_id"))
            if verdict is not None and verdict.verdict is not VerifierVerdict.INCOMPLETE:
                judged_siblings.append(verdict)
    if not judged_siblings:
        return None
    failed = sum(1 for v in judged_siblings if v.verdict is VerifierVerdict.FAIL)
    return failed, len(judged_siblings)


def run_ids(summaries: list[BatchSummary]) -> list[str]:
    """Every run a recorded batch names, entries and replayed siblings alike."""
    ids = [e.run_id for s in summaries for e in s.entries if e.run_id]
    ids += [
        sibling["run_id"]
        for s in summaries
        for sibling in s.metadata.get("siblings") or []
        if sibling.get("run_id")
    ]
    return sorted(set(ids))


def pair_table(pairs: list[PairVerdict]) -> list[dict[str, Any]]:
    """The pairs as plain rows for ``result.metadata``, exclusions and reasons included."""
    return [
        {
            "kind": p.kind,
            "model": p.model,
            "artifact": p.artifact,
            "task_id": p.task_id,
            "control": p.control,
            "fork_step": p.fork_step,
            "static_clear": p.static_clear,
            "clear_seeds": p.clear_seeds,
            "completed_seeds": p.completed_seeds,
            "live_clear": p.live_clear,
            "agrees": p.agrees,
            "excluded": p.excluded,
        }
        for p in pairs
    ]
