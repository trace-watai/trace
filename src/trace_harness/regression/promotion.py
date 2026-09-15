"""Promote accepted controls only after replaying the proposed active library."""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from collections.abc import Callable
from pathlib import Path

from trace_harness.environment.control_library import (
    ArtifactRef,
    ControlLibrary,
    LibraryEntry,
    LibraryProvenance,
    StatusChange,
    check_acceptance,
    library_lock,
    load_library,
    write_library,
)
from trace_harness.environment.controls import ControlInstance, resolve_control
from trace_harness.failure_bundles.schemas import RepairPackage
from trace_harness.regression.repair_validation import ControlVerdict, RepairValidation, ReRun
from trace_harness.regression.schemas import RegressionArtifact
from trace_harness.runner.result import RunResult, RunStatus
from trace_harness.tracing import artifact_store as names
from trace_harness.verifiers.base import VerifierResult

RUN_EVIDENCE = (
    names.RUN_RESULT,
    names.RUN_CONFIG,
    names.TASK_SPEC,
    names.INITIAL_STATE,
    names.FINAL_STATE,
    names.TRACE,
    names.VERIFIER_RESULT,
)


def _safe_run_id(run_id: str) -> str:
    if Path(run_id).name != run_id or run_id in ("", ".", "..") or "\\" in run_id or ":" in run_id:
        raise ValueError("invalid evidence run ID")
    return run_id


class LibraryGateError(ValueError):
    """A completed validation or proposed-library regression failed its gate."""


def _capture_run(files: dict[str, bytes], prefix: str, run_dir: Path) -> None:
    for name in RUN_EVIDENCE:
        files[f"{prefix}/{name}"] = (run_dir / name).read_bytes()


def _check_rerun(run_dir: Path, rerun: ReRun, control: ControlInstance) -> None:
    result = RunResult.model_validate_json((run_dir / names.RUN_RESULT).read_bytes())
    verifier = VerifierResult.model_validate_json((run_dir / names.VERIFIER_RESULT).read_bytes())
    config = json.loads((run_dir / names.RUN_CONFIG).read_bytes())
    installed = config.get("metadata", {}).get("controls", [])
    if (
        result.run_id != rerun.run_id
        or verifier.run_id != rerun.run_id
        or result.task_id != rerun.task_id
        or result.status is not RunStatus.COMPLETED
        or verifier.verdict.value.upper() != rerun.verdict
        or sorted(c.check_id for c in verifier.failed_checks) != sorted(rerun.failed_checks)
        or len(installed) != 1
        or installed[0].get("control_id") != control.control_id
        or installed[0].get("guardrail_ref") != control.guardrail_ref
        or installed[0].get("rule_ref") != control.rule_ref.model_dump(mode="json")
    ):
        raise ValueError(f"validation evidence does not match control {control.control_id!r}")


def _check_activation(
    regression: RegressionArtifact, run_dirs: list[Path], controls: list[ControlInstance]
) -> None:
    """Check the persisted gate evidence before it can authorize a library write."""
    if len(run_dirs) != 1 + len(regression.positive_sibling_tests):
        raise LibraryGateError("library regression did not produce all required runs")
    for index, run_dir in enumerate(run_dirs):
        run = RunResult.model_validate_json((run_dir / names.RUN_RESULT).read_bytes())
        verdict = VerifierResult.model_validate_json((run_dir / names.VERIFIER_RESULT).read_bytes())
        config = json.loads((run_dir / names.RUN_CONFIG).read_bytes())
        failed = {c.check_id for c in verdict.failed_checks}
        pinned = set(regression.verifier_checks)
        if (
            run.status is not RunStatus.COMPLETED
            or verdict.run_id != run.run_id
            or verdict.verdict.value == "incomplete"
            or config.get("metadata", {}).get("controls")
            != [c.model_dump(mode="json") for c in controls]
            or (index > 0 and not verdict.passed)
            or (
                index == 0
                and (
                    pinned.intersection(failed)
                    or any(
                        c.blocks_release and c.check_id not in pinned for c in verdict.failed_checks
                    )
                )
            )
        ):
            raise LibraryGateError(f"library regression evidence did not pass: {run_dir}")


def commit_controls(
    library_path: Path,
    artifact_path: Path,
    validation_path: Path,
    candidates: list[ControlInstance],
    replay_gate: Callable[[Path, list[ControlInstance]], list[Path]],
) -> ControlLibrary:
    """Retain evidence and append accepted entries as one atomic library revision.

    The callback runs the originating regression and siblings with the proposed
    complete library, returning their run directories or raising LibraryGateError.
    Existing active regressions are checked too, so a new entry cannot silently
    invalidate an earlier promotion.
    """
    artifact = RegressionArtifact.model_validate_json(artifact_path.read_bytes())
    package_path = artifact_path.with_name(names.REPAIR_PACKAGE)
    package = RepairPackage.model_validate_json(package_path.read_bytes())
    validation = RepairValidation.model_validate_json(validation_path.read_bytes())
    source = RunResult.model_validate_json(artifact_path.with_name(names.RUN_RESULT).read_bytes())
    original = VerifierResult.model_validate_json(
        artifact_path.with_name(names.VERIFIER_RESULT).read_bytes()
    )
    if original.run_id != source.run_id or set(artifact.verifier_checks) != {
        c.check_id for c in original.failed_checks
    }:
        raise ValueError("regression checks do not match the originating verifier evidence")

    accepted_ids = {
        c.control_id for c in validation.controls if c.verdict is ControlVerdict.ACCEPTED
    }
    accepted = [c.model_copy(deep=True) for c in candidates if c.control_id in accepted_ids]
    if not accepted:
        raise LibraryGateError("no accepted controls to commit")
    if len({c.control_id for c in accepted}) != len(accepted):
        raise ValueError("cannot commit duplicate control IDs")

    source_prefix = f"source/{_safe_run_id(source.run_id)}"
    files = {
        f"{source_prefix}/{names.REGRESSION_ARTIFACT}": artifact_path.read_bytes(),
        f"{source_prefix}/{names.REPAIR_PACKAGE}": package_path.read_bytes(),
        names.REPAIR_VALIDATION: validation_path.read_bytes(),
    }
    _capture_run(files, source_prefix, artifact_path.parent)
    for control in accepted:
        control.provenance.run_id = source.run_id
        resolve_control(control)
        check_acceptance(control, source, package, artifact, validation)
        verdict = next(c for c in validation.controls if c.control_id == control.control_id)
        for rerun in [verdict.originating_rerun, *verdict.sibling_reruns]:
            assert rerun is not None
            run_dir = validation_path.parent.parent / _safe_run_id(rerun.run_id)
            _check_rerun(run_dir, rerun, control)
            _capture_run(files, f"validation/{rerun.run_id}", run_dir)

    with library_lock(library_path):
        library = load_library(library_path) if library_path.exists() else ControlLibrary()
        existing = {entry.control.control_id for entry in library.entries}
        duplicates = existing.intersection(c.control_id for c in accepted)
        if duplicates:
            raise ValueError(f"control IDs already have library history: {sorted(duplicates)}")
        proposed = sorted([*library.active_controls(), *accepted], key=lambda c: c.control_id)
        artifacts = [
            entry.provenance.regression_artifact.resolve(library_path.parent)
            for entry in library.entries
            if entry.status == "active"
        ]
        artifacts.append(artifact_path)
        checks = []
        for regression_path in dict.fromkeys(artifacts):
            regression = RegressionArtifact.model_validate_json(regression_path.read_bytes())
            run_dirs = replay_gate(regression_path, proposed)
            _check_activation(regression, run_dirs, proposed)
            for run_dir in run_dirs:
                _capture_run(files, f"activation/{run_dir.name}", run_dir)
            checks.append(
                {
                    "source_run_id": regression.source_run_id,
                    "test_name": regression.test_name,
                    "run_ids": [p.name for p in run_dirs],
                }
            )
        files["activation_check.json"] = (
            json.dumps(
                {
                    "schema_version": "0.1.0",
                    "controls": [c.model_dump(mode="json") for c in proposed],
                    "regressions": checks,
                },
                indent=2,
            )
            + "\n"
        ).encode()

        # Publish evidence first and the manifest last. A failed manifest write
        # can leave no active entry pointing at partially written evidence.
        relative = Path("evidence") / uuid.uuid4().hex
        evidence_dir = library_path.parent / relative
        evidence_dir.mkdir(parents=True)
        try:
            refs = {}
            for name, data in files.items():
                destination = evidence_dir / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
                refs[name] = ArtifactRef(
                    path=(relative / name).as_posix(), sha256=hashlib.sha256(data).hexdigest()
                )
            provenance = LibraryProvenance(
                source_run=refs[f"{source_prefix}/{names.RUN_RESULT}"],
                repair_package=refs[f"{source_prefix}/{names.REPAIR_PACKAGE}"],
                repair_validation=refs[names.REPAIR_VALIDATION],
                regression_artifact=refs[f"{source_prefix}/{names.REGRESSION_ARTIFACT}"],
                activation_check=refs["activation_check.json"],
                evidence=[
                    ref
                    for name, ref in refs.items()
                    if name
                    not in {
                        f"{source_prefix}/{names.RUN_RESULT}",
                        f"{source_prefix}/{names.REPAIR_PACKAGE}",
                        names.REPAIR_VALIDATION,
                        f"{source_prefix}/{names.REGRESSION_ARTIFACT}",
                        "activation_check.json",
                    }
                ],
            )
            library.entries.extend(
                LibraryEntry(
                    control=control,
                    provenance=provenance,
                    history=[
                        StatusChange(
                            status="active",
                            reason="accepted validation and proposed-library replay passed",
                        )
                    ],
                )
                for control in accepted
            )
            write_library(library_path, library)
        except BaseException:
            shutil.rmtree(evidence_dir)
            raise
    return library
