"""Versioned controls with retained evidence and append-only status history (#147).

Each entry records the basis its acceptance rests on. A control accepted
against a ``static_ok`` artifact whose recorded basis still classifies as
``static_ok`` is gating, following the collector's reading of ADR-0002; that
label is predicted by the materializer until #159 measures it. Any other
accepted control may still enter the library, and is installed wherever the
library is loaded, but it is recorded as advisory. Its evidence is a replay
ADR-0002 keeps advisory, so a suite run with it installed is a measurement of
the control, and nothing may report an advisory entry as proven.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from trace_harness.environment.controls import ControlInstance, resolve_control
from trace_harness.failure_bundles.schemas import RepairPackage
from trace_harness.regression.repair_validation import (
    ControlValidation,
    ControlVerdict,
    RepairValidation,
    VerdictStanding,
    basis_supports_label,
    gating_refusal,
    predictor_of,
)
from trace_harness.regression.schemas import RegressionArtifact, ReplayMode, ReplayModePredictor
from trace_harness.runner.result import RunResult
from trace_harness.tracing import artifact_store as names
from trace_harness.tracing.events import utc_now

# 0.2.0: entries record their acceptance basis. A 0.1.0 entry has none; it
# reads as advisory with its replay_mode not recorded.
CONTROL_LIBRARY_SCHEMA_VERSION = "0.2.0"
DEFAULT_CONTROL_LIBRARY = Path("fixtures/controls/library.json")


class ArtifactRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    def resolve(self, root: Path) -> Path:
        relative = PurePosixPath(self.path)
        if (
            not relative.parts
            or relative.is_absolute()
            or ".." in relative.parts
            or "\\" in self.path
            or ":" in self.path
        ):
            raise ValueError(f"evidence path must stay inside the library directory: {self.path}")
        path = (root / relative).resolve()
        if not path.is_relative_to(root.resolve()):
            raise ValueError(f"evidence path escapes the library directory: {self.path}")
        return path

    def read(self, root: Path) -> bytes:
        path = self.resolve(root)
        if not path.is_file():
            raise FileNotFoundError(f"control evidence file not found: {self.path}")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != self.sha256:
            raise ValueError(f"control evidence changed: {self.path}")
        return data


class LibraryProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_run: ArtifactRef
    repair_package: ArtifactRef
    repair_validation: ArtifactRef
    regression_artifact: ArtifactRef
    activation_check: ArtifactRef
    # Task, trace, configuration, state, and verifier artifacts for source,
    # individual validation, and combined-library regression runs.
    evidence: list[ArtifactRef] = Field(min_length=1)

    def refs(self) -> list[ArtifactRef]:
        return [
            self.source_run,
            self.repair_package,
            self.repair_validation,
            self.regression_artifact,
            self.activation_check,
            *self.evidence,
        ]


class StatusChange(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    status: Literal["active", "rolled_back"]
    reason: str = Field(min_length=1)
    at: datetime = Field(default_factory=utc_now)


class AcceptanceBasis(BaseModel):
    """The replay label an entry's accepted verdict was reached under.

    An entry written before 0.2.0 has no basis. Nothing at acceptance time
    recorded which label the verdict relied on, so it reads as not recorded
    (``replay_mode`` None) and advisory, whatever label its retained artifact
    carries now. ``check_acceptance`` holds a recorded basis to the artifact
    and refuses a gating basis that records no label.
    """

    model_config = ConfigDict(extra="forbid")

    replay_mode: ReplayMode | None = None
    # Who produced the label, from the artifact's replay_mode_basis. Every
    # gating basis today names the materializer's fixed rule, so its label is
    # predicted until #159 measures one.
    predicted_by: ReplayModePredictor | None = None
    standing: VerdictStanding = "advisory"

    @classmethod
    def for_artifact(cls, artifact: RegressionArtifact) -> AcceptanceBasis:
        """The basis an artifact supports: gating only past ``gating_refusal``."""
        return cls(
            replay_mode=artifact.replay_mode,
            predicted_by=predictor_of(artifact),
            standing="gating" if gating_refusal(artifact) is None else "advisory",
        )


class LibraryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    control: ControlInstance
    provenance: LibraryProvenance
    status: Literal["active", "rolled_back"] = "active"
    history: list[StatusChange] = Field(min_length=1)
    acceptance: AcceptanceBasis = Field(default_factory=AcceptanceBasis)

    @model_validator(mode="after")
    def consistent_history(self) -> LibraryEntry:
        states = [event.status for event in self.history]
        if states not in (["active"], ["active", "rolled_back"]) or states[-1] != self.status:
            raise ValueError("control status must match its commit/rollback history")
        return self


class ControlLibrary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1.0", "0.2.0"] = CONTROL_LIBRARY_SCHEMA_VERSION
    entries: list[LibraryEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_ids(self) -> ControlLibrary:
        ids = [entry.control.control_id for entry in self.entries]
        if len(ids) != len(set(ids)):
            raise ValueError("control library contains duplicate control IDs")
        return self

    def active_controls(self) -> list[ControlInstance]:
        return sorted(
            [
                entry.control.model_copy(deep=True)
                for entry in self.entries
                if entry.status == "active"
            ],
            key=lambda control: control.control_id,
        )


class ActivationRegression(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_run_id: str
    test_name: str
    run_ids: list[str] = Field(min_length=1)


class ActivationCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1.0"] = "0.1.0"
    controls: list[ControlInstance] = Field(min_length=1)
    regressions: list[ActivationRegression] = Field(min_length=1)


def check_acceptance(
    control: ControlInstance,
    source: RunResult,
    package: RepairPackage,
    artifact: RegressionArtifact,
    validation: RepairValidation,
    basis: AcceptanceBasis,
) -> None:
    """Bind an accepted verdict to this exact control and originating failure.

    A recorded basis must name the artifact's own ``replay_mode`` and
    ``predicted_by`` and the standing the artifact supports. A gating
    acceptance therefore needs a ``static_ok`` label whose recorded basis
    classifies as ``static_ok`` (``gating_refusal``), and an advisory one
    cannot be recorded as gating. A verdict's recorded label must be the
    artifact's, down to whether its basis supports it. A label that was never
    recorded, on the verdict or on the basis, is not compared with the
    artifact, and a verdict or basis without one can only be advisory and
    cannot name a predictor.
    """
    run_id = source.run_id
    if {package.run_id, artifact.source_run_id, validation.run_id, control.provenance.run_id} != {
        run_id
    }:
        raise ValueError("control provenance has mismatched originating run IDs")
    if source.task_id != package.task_id or validation.test_name != artifact.test_name:
        raise ValueError("control provenance has mismatched task or regression identity")
    if validation.controls_source != "repair_package":
        raise ValueError("committing a control requires repair-package validation")
    prescriptions = [c for c in package.controls if c.name == control.provenance.repair_control]
    verdicts = [c for c in validation.controls if c.control_id == control.control_id]
    if len(prescriptions) != 1 or len(verdicts) != 1:
        raise ValueError("control must have exactly one prescription and validation verdict")
    prescription, verdict = prescriptions[0], verdicts[0]
    linked = set(prescription.linked_verifier_checks)
    if (
        verdict.verdict is not ControlVerdict.ACCEPTED
        or verdict.control != prescription.name
        or verdict.guardrail_ref != control.guardrail_ref
        or not linked
        or not linked.issubset(artifact.verifier_checks)
        or verdict.originating_rerun is None
        or verdict.originating_rerun.verdict == "INCOMPLETE"
        or set(verdict.originating_rerun.cleared_checks) != linked
        or linked.intersection(verdict.originating_rerun.failed_checks)
        or len(verdict.sibling_reruns) != len(artifact.positive_sibling_tests)
        or any(r.verdict != "PASS" for r in verdict.sibling_reruns)
    ):
        raise ValueError(f"control {control.control_id!r} lacks complete accepted validation")
    _check_replay_label(control.control_id, verdict, artifact)
    _check_basis(control.control_id, basis, artifact)


def _check_replay_label(
    control_id: str, verdict: ControlValidation, artifact: RegressionArtifact
) -> None:
    """A verdict's recorded label must be the artifact's; an unrecorded one is not compared."""
    replay_mode, predicted_by = verdict.replay_mode, verdict.predicted_by
    if replay_mode is None:
        if predicted_by is not None or verdict.label_supported:
            raise ValueError(
                f"control {control_id!r} records a predictor or a supported label "
                "without the replay_mode it was validated under"
            )
        return
    if replay_mode != artifact.replay_mode:
        raise ValueError(
            f"control {control_id!r} was validated as {replay_mode} "
            f"but the artifact is {artifact.replay_mode}"
        )
    if predicted_by != predictor_of(artifact):
        raise ValueError(
            f"control {control_id!r} was validated on a label from {predicted_by} "
            f"but the artifact's label is from {predictor_of(artifact)}"
        )
    if verdict.label_supported != basis_supports_label(artifact):
        recorded = "supported" if verdict.label_supported else "did not support"
        now = "does not" if verdict.label_supported else "does"
        raise ValueError(
            f"control {control_id!r} was validated on a label its basis {recorded}, "
            f"but the artifact's recorded basis {now} support it"
        )


def _check_basis(control_id: str, basis: AcceptanceBasis, artifact: RegressionArtifact) -> None:
    """A recorded basis must match the artifact and claim exactly the standing it supports."""
    if basis.replay_mode is None:
        if basis.standing != "advisory":
            raise ValueError(
                f"control {control_id!r} records a {basis.standing} acceptance "
                "without the replay_mode it relied on"
            )
        if basis.predicted_by is not None:
            raise ValueError(
                f"control {control_id!r} records a label from {basis.predicted_by} "
                "without the replay_mode it relied on"
            )
        return
    if basis.replay_mode != artifact.replay_mode:
        raise ValueError(
            f"control {control_id!r} records replay_mode {basis.replay_mode} "
            f"but the artifact is {artifact.replay_mode}"
        )
    refusal = gating_refusal(artifact)
    if basis.standing == "gating" and refusal is not None:
        raise ValueError(f"control {control_id!r} records a gating acceptance but {refusal}")
    if basis.standing == "advisory" and refusal is None:
        raise ValueError(
            f"control {control_id!r} records an advisory acceptance "
            "but the artifact supports gating"
        )
    if basis.predicted_by != predictor_of(artifact):
        raise ValueError(
            f"control {control_id!r} records a label from {basis.predicted_by} "
            f"but the artifact's label is from {predictor_of(artifact)}"
        )


def load_library(path: Path | str, *, resolve_active: bool = True) -> ControlLibrary:
    """Validate all retained provenance, including entries that were rolled back."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"control library file not found: {path}")
    library = ControlLibrary.model_validate_json(path.read_bytes())
    for entry in library.entries:
        refs = entry.provenance
        # Read all files first so even evidence outside the summary is checked.
        for ref in refs.refs():
            ref.read(path.parent)
        validation = RepairValidation.model_validate_json(refs.repair_validation.read(path.parent))
        check_acceptance(
            entry.control,
            RunResult.model_validate_json(refs.source_run.read(path.parent)),
            RepairPackage.model_validate_json(refs.repair_package.read(path.parent)),
            RegressionArtifact.model_validate_json(refs.regression_artifact.read(path.parent)),
            validation,
            entry.acceptance,
        )
        _require_retained_runs(path.parent, entry, validation)
        if resolve_active and entry.status == "active":
            resolve_control(entry.control)
    return library


def _require_retained_runs(root: Path, entry: LibraryEntry, validation: RepairValidation) -> None:
    """Summary run IDs must resolve to complete snapshots in the retained evidence."""
    refs = {ref.path: ref for ref in entry.provenance.refs()}
    run_dirs = {}
    for name, ref in refs.items():
        if PurePosixPath(name).name == names.RUN_RESULT:
            result = RunResult.model_validate_json(ref.read(root))
            if result.run_id in run_dirs:
                raise ValueError(f"duplicate retained run ID: {result.run_id}")
            run_dirs[result.run_id] = PurePosixPath(name).parent
    accepted = next(c for c in validation.controls if c.control_id == entry.control.control_id)
    required = {entry.control.provenance.run_id}
    required.update(r.run_id for r in [accepted.originating_rerun, *accepted.sibling_reruns] if r)
    activation = ActivationCheck.model_validate_json(entry.provenance.activation_check.read(root))
    if entry.control not in activation.controls:
        raise ValueError("activation check does not include the committed control")
    if not any(r.source_run_id == entry.control.provenance.run_id for r in activation.regressions):
        raise ValueError("activation check omits the originating regression")
    for regression in activation.regressions:
        required.update(regression.run_ids)
    for run_id in required:
        if run_id not in run_dirs:
            raise ValueError(f"missing retained run evidence: {run_id}")
        for name in (
            names.RUN_CONFIG,
            names.TASK_SPEC,
            names.INITIAL_STATE,
            names.FINAL_STATE,
            names.TRACE,
            names.VERIFIER_RESULT,
        ):
            if str(run_dirs[run_id] / name) not in refs:
                raise ValueError(f"missing retained {name} for run {run_id}")


@contextmanager
def library_lock(path: Path) -> Iterator[None]:
    """Fail on concurrent writers instead of losing a commit or rollback."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + ".lock")
    try:
        handle = lock.open("x")
    except FileExistsError:
        raise ValueError(f"control library is locked: {lock}") from None
    try:
        with handle:
            handle.write(str(os.getpid()))
        yield
    finally:
        lock.unlink()


def write_library(path: Path, library: ControlLibrary) -> None:
    """Atomically publish a complete revision; callers must hold library_lock.

    A revision is written at the current schema. An older library gains its
    entries' default acceptance basis explicitly the first time it is written.
    """
    library = library.model_copy(update={"schema_version": CONTROL_LIBRARY_SCHEMA_VERSION})
    ControlLibrary.model_validate(library.model_dump())
    payload = json.dumps(library.model_dump(mode="json"), indent=2) + "\n"
    fd, name = tempfile.mkstemp(dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def rollback_control(path: Path | str, control_id: str, reason: str) -> ControlLibrary:
    path = Path(path)
    event = StatusChange(status="rolled_back", reason=reason)
    with library_lock(path):
        library = load_library(path, resolve_active=False)
        entry = next((e for e in library.entries if e.control.control_id == control_id), None)
        if entry is None or entry.status != "active":
            raise ValueError(f"control {control_id!r} is not active in this library")
        entry.history.append(event)
        entry.status = "rolled_back"
        write_library(path, library)
    return library
