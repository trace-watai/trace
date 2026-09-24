"""Collect pinned regressions and run the existing replay path as an offline CI gate.

Baseline reproduction and positive siblings always gate. Control replay only
gates for an explicit static_ok label; unlabeled/live_required results remain
advisory. This reader accepts #156's labels without generating or changing them.

A generated suite run that reproduced an earlier card holds a ``bundle_ref.json``
pointer and no regression artifact of its own (#211). It counts as covered when
the run it points to holds one. It is not an artifact itself, since discovery finds
the first occurrence's artifact once, so a key is replayed once however many runs
repeated it.

Given an experiments directory, the collector also recomputes every retained
experiment's frozen set (#195). Drift there is reported and recorded in the
summary without failing the gate; a retained experiment that no longer loads
fails it.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from time import monotonic
from typing import Literal

from pydantic import BaseModel, Field, computed_field, model_validator

from trace_harness.regression.report import ReplayReport
from trace_harness.regression.schemas import RegressionArtifact
from trace_harness.runner.batch import BatchRunner
from trace_harness.runner.batch import summary_path as batch_summary_path
from trace_harness.runner.experiment import ExperimentResult, ExperimentSpec
from trace_harness.runner.frozen_set import FrozenFileChange, check_frozen_set
from trace_harness.runner.suite import load_suite
from trace_harness.tracing.artifact_store import (
    EXPERIMENT_RESULT,
    EXPERIMENT_SPEC,
    REGRESSION_ARTIFACT,
    REGRESSION_GATE_SUMMARY,
    ArtifactStore,
    _atomic_write_text,
)

SUMMARY_NAME = REGRESSION_GATE_SUMMARY
ReplayMode = Literal["static_ok", "live_required", "unlabeled"]


class _CollectedArtifact(RegressionArtifact):
    # RegressionArtifact ignores unknown fields until #156 lands. Read its
    # future label here explicitly; a typo must never silently weaken a gate.
    replay_mode: ReplayMode = "unlabeled"
    blocks_release: bool = Field(strict=True)

    @model_validator(mode="after")
    def _has_assertions(self) -> _CollectedArtifact:
        if not self.test_name.strip():
            raise ValueError("regression test_name must not be empty")
        if self.blocks_release and (
            not self.verifier_checks or any(not check.strip() for check in self.verifier_checks)
        ):
            raise ValueError("blocking regression must pin at least one verifier check")
        return self


class CollectorEntry(BaseModel):
    artifact_path: str
    test_name: str | None = None
    blocks_release: bool | None = None
    replay_mode: ReplayMode = "unlabeled"
    source_sha256: str | None = None
    evidence_dir: str | None = None
    baseline: ReplayReport | None = None
    control: ReplayReport | None = None
    control_status: Literal["confirmed", "failed", "advisory", "skipped"] = "skipped"
    error: str | None = None
    control_error: str | None = None


class ExperimentFreezeEntry(BaseModel):
    """One retained experiment's frozen set against the tree the gate ran on."""

    experiment_id: str
    status: Literal["matches", "drifted", "not_recorded"]
    # The result was itself recorded over drift with --allow-drift.
    recorded_drifted: bool = False
    changes: list[FrozenFileChange] = Field(default_factory=list)


class CollectorSummary(BaseModel):
    schema_version: Literal["0.1.0"] = "0.1.0"
    artifacts_found: int = 0
    blocking: int = 0
    reproduced: int = 0
    siblings_passed: int = 0
    not_reproduced: list[str] = Field(default_factory=list)
    siblings_failed: list[tuple[str, str]] = Field(default_factory=list)
    controls_confirmed: int = 0
    controls_advisory: int = 0
    controls_failed: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    malformed: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    runs_dir: str
    collection_dir: str
    suite_summary_path: str | None = None
    duration_s: float = 0.0
    entries: list[CollectorEntry] = Field(default_factory=list)
    experiments: list[ExperimentFreezeEntry] = Field(default_factory=list)
    # Warnings only: never part of exit_code (see _check_experiments).
    experiments_drifted: list[str] = Field(default_factory=list)

    @computed_field
    @property
    def exit_code(self) -> int:
        if self.malformed:
            return 2
        if self.not_reproduced or self.siblings_failed or self.controls_failed or self.errors:
            return 1
        return 0


def _discover(source: Path, *, excluded: Path | None = None) -> list[Path]:
    if source.is_file():
        return [source]
    if not source.is_dir():
        raise ValueError(f"regression input path not found: {source}")

    def unreadable(error: OSError) -> None:
        raise error

    paths = []
    # os.walk includes broken file symlinks and exposes directory-read errors;
    # glob's literal-name selector can silently omit those inputs.
    for directory, children, files in os.walk(source, onerror=unreadable, followlinks=False):
        current = Path(directory)
        children[:] = sorted(
            child
            for child in children
            if excluded is None or not (current / child).is_relative_to(excluded)
        )
        if REGRESSION_ARTIFACT in files:
            paths.append(current / REGRESSION_ARTIFACT)
    return sorted(paths)


def _covered(store: ArtifactStore, run_id: str | None) -> bool:
    """Whether a failed run's regression artifact exists, in its own directory or its card's."""
    if run_id is None:
        return False
    home = store.bundle_home(run_id)
    return home is not None and store.exists(home, REGRESSION_ARTIFACT)


def _replay(artifact: Path, evidence_dir: Path, *, apply_control: bool) -> ReplayReport:
    # Reuse the command's implementation, not its printed output or shell
    # replay_command. #146's per-control report can replace this one seam.
    from trace_harness.cli import _replay_with_report

    evidence_dir.mkdir(parents=True, exist_ok=True)
    with (evidence_dir / "replay.log").open("w", encoding="utf-8") as log:
        with redirect_stdout(log):
            return _replay_with_report(
                artifact, ArtifactStore(evidence_dir), apply_control=apply_control
            )


def collect_regressions(
    source: Path | str,
    store: ArtifactStore,
    *,
    suite_path: Path | str | None = None,
    experiments_path: Path | str | None = None,
) -> CollectorSummary:
    """Run a collection, retain its evidence, and atomically write the summary.

    Every invocation gets a fresh workspace. The root summary points to the
    latest collection; each workspace retains its own summary and artifacts.
    Discovery snapshots the file list before suite generation/replay and excludes our
    working directories when scanning an ancestor, so repeated gates don't
    collect their own copied inputs. Explicit paths inside them remain usable.
    """
    started = monotonic()
    source = Path(source).resolve()
    runs_dir = store.runs_dir.resolve()
    collections = runs_dir / "regression-collections"
    discovery_error = None
    try:
        paths = _discover(
            source, excluded=None if source.is_relative_to(collections) else collections
        )
    except (OSError, ValueError) as exc:
        paths = []
        discovery_error = str(exc)

    # Discover first: creating the output must not make a missing input appear
    # to be an empty, valid directory when input and output paths overlap.
    collections.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="collection-", dir=collections))
    summary = CollectorSummary(runs_dir=str(runs_dir), collection_dir=str(work))
    if discovery_error is not None:
        summary.malformed.append(str(source))
        summary.errors.append(discovery_error)

    if suite_path is not None:
        try:
            suite = load_suite(suite_path)
            if any(config.provider != "fixture" for config in suite.agent_configs):
                raise ValueError(
                    "regression collection suites must use the offline fixture provider"
                )
            generated = ArtifactStore(work / "generated")
            batch = BatchRunner(generated).run(suite)
            summary.suite_summary_path = str(batch_summary_path(generated.runs_dir, batch.batch_id))
            for entry in batch.entries:
                if entry.status != "completed" or entry.verdict not in {"pass", "fail"}:
                    summary.errors.append(
                        f"suite {entry.agent_label}/{entry.task_id}: {entry.error or entry.status}"
                    )
                elif entry.verdict == "fail" and not _covered(generated, entry.run_id):
                    summary.errors.append(
                        f"suite {entry.task_id}: failed run has no regression artifact"
                    )
            paths.extend(_discover(generated.runs_dir))
        except (OSError, KeyError, ValueError) as exc:
            summary.malformed.append(str(suite_path))
            summary.errors.append(f"suite: {exc}")
        except Exception as exc:  # noqa: BLE001 — retain an actionable gate summary
            summary.errors.append(f"suite: {exc}")

    summary.artifacts_found = len(paths)
    for index, path in enumerate(paths, 1):
        entry = CollectorEntry(artifact_path=str(path))
        summary.entries.append(entry)
        try:
            raw = path.read_bytes()
            artifact = _CollectedArtifact.model_validate(json.loads(raw))
            entry.test_name = artifact.test_name
            entry.blocks_release = artifact.blocks_release
            entry.replay_mode = artifact.replay_mode
            entry.source_sha256 = hashlib.sha256(raw).hexdigest()
            if not artifact.blocks_release:
                summary.skipped.append(artifact.test_name)
                continue
            summary.blocking += 1
            evidence = work / f"artifact-{index:04d}"
            evidence.mkdir()
            entry.evidence_dir = str(evidence)
            # Freeze exactly the bytes we parsed, including future label basis
            # fields. Never edit the original artifact or execute replay_command.
            pinned = evidence / REGRESSION_ARTIFACT
            pinned.write_bytes(raw)
            entry.baseline = _replay(pinned, evidence / "baseline", apply_control=False)
        except (OSError, KeyError, ValueError) as exc:
            entry.error = str(exc)
            summary.malformed.append(str(path))
            continue
        except Exception as exc:  # noqa: BLE001 — isolate a failed replay, keep collecting
            entry.error = str(exc)
            summary.errors.append(f"{path}: {exc}")
            continue

        baseline = entry.baseline
        if baseline.reproduced:
            summary.reproduced += 1
        else:
            summary.not_reproduced.append(artifact.test_name)
        for sibling in baseline.siblings:
            if sibling.passed:
                summary.siblings_passed += 1
            else:
                summary.siblings_failed.append((artifact.test_name, sibling.test_name))

        try:
            entry.control = _replay(pinned, evidence / "control", apply_control=True)
        except Exception as exc:  # noqa: BLE001 — control errors retain advisory/gating semantics
            entry.control_error = str(exc)
        if artifact.replay_mode == "static_ok":
            baseline_valid = baseline.reproduced and all(s.passed for s in baseline.siblings)
            if baseline_valid and entry.control is not None and entry.control.control_confirmed:
                entry.control_status = "confirmed"
                summary.controls_confirmed += 1
            else:
                entry.control_status = "failed"
                summary.controls_failed.append(artifact.test_name)
                if not baseline_valid and entry.control_error is None:
                    entry.control_error = "baseline gate failed; control cannot be confirmed"
        else:
            entry.control_status = "advisory"
            summary.controls_advisory += 1

    if experiments_path is not None:
        _check_experiments(Path(experiments_path), summary)

    summary.duration_s = round(monotonic() - started, 3)
    content = summary.model_dump_json(indent=2) + "\n"
    _atomic_write_text(work / SUMMARY_NAME, content)
    _atomic_write_text(runs_dir / SUMMARY_NAME, content)
    return summary


def _check_experiments(directory: Path, summary: CollectorSummary) -> None:
    """Recompute each retained experiment's frozen set against the working tree.

    Drift is a warning here and blocks only at record time. A retained
    experiment was checked when it was recorded; a later reviewed edit to the
    verifier makes it stale without making its recorded numbers wrong. Failing
    the gate on that would turn CI red on every verifier change until each
    retained baseline was re-run. The cost is that CI never forces a
    re-baseline, so the warning and ``experiments_drifted`` are the record.
    """
    if not directory.is_dir():
        summary.malformed.append(str(directory))
        summary.errors.append(f"experiments directory not found: {directory}")
        return
    for plan in sorted(directory.glob(f"*/{EXPERIMENT_SPEC}")):
        try:
            spec = ExperimentSpec.model_validate_json(plan.read_text(encoding="utf-8"))
            result_path = plan.parent / EXPERIMENT_RESULT
            result = (
                ExperimentResult.model_validate_json(result_path.read_text(encoding="utf-8"))
                if result_path.is_file()
                else None
            )
        except (OSError, ValueError) as exc:
            summary.malformed.append(str(plan.parent))
            summary.errors.append(f"{plan.parent}: {exc}")
            continue
        manifest = spec.frozen_manifest
        entry = ExperimentFreezeEntry(
            experiment_id=spec.experiment_id,
            status="not_recorded",
            recorded_drifted=result is not None and result.frozen_set_drifted,
        )
        if manifest.frozen_set is not None:
            entry.changes = check_frozen_set(
                manifest.frozen_set,
                Path.cwd(),
                suite_id=manifest.suite_id,
                labels_path=manifest.labels_path,
            )
            entry.status = "drifted" if entry.changes else "matches"
        if entry.changes:
            summary.experiments_drifted.append(spec.experiment_id)
        summary.experiments.append(entry)
