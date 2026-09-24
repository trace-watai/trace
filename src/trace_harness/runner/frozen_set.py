"""The frozen set: the evaluator an experiment must not change under itself (#195).

An experiment's plan says what stays fixed while its conditions run. #155
stored a hash of the fixtures and nothing else, so an experiment could start
under one verifier, record under another, and the result would read like any
other. This module hashes the evaluator file by file when the plan is frozen,
and recomputes the same hashes when the result is recorded, so a change is
refused with the files named.

The components, every path relative to the repository root:

``verifiers``    ``src/trace_harness/verifiers/``, the ground truth.
``environment``  ``src/trace_harness/environment/``, the mock tools and state.
``attribution``  ``src/trace_harness/attribution/``, the attribution scorer:
                 ``HeuristicAttributor``, the schema it emits and the
                 validation it runs on its own output.
``suite``        ``fixtures/suites/{suite_id}.json``.
``fixtures``     ``fixtures/``, the control library and its evidence
                 included, apart from the ``index.json`` that re-verifying a
                 retained evidence run writes at
                 ``fixtures/controls/evidence/*/*/index.json``. Brief 001
                 allows new control entries only before the first live run,
                 and the plan is frozen before any condition runs, so a
                 library change between freeze and record is drift.
``labels``       the plan's ``labels_path``, when it names one.

A file hash is sha256 over the file's bytes with CRLF folded to LF, keyed by
its repo-relative POSIX path. A component digest is sha256 over the sorted
``path, hash`` list. Neither a checkout's line endings nor the order a
filesystem lists a directory changes a digest. Bytecode caches and editor
files are skipped.
"""

from __future__ import annotations

import hashlib
import os
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict

from trace_harness.runner.suite import load_suite

CODE_COMPONENTS = {
    "verifiers": "src/trace_harness/verifiers",
    "environment": "src/trace_harness/environment",
    "attribution": "src/trace_harness/attribution",
}
FIXTURES_ROOT = "fixtures"
# Matched one path segment at a time, as .gitignore reads the same pattern.
FIXTURES_EXCLUDED = ("fixtures/controls/evidence/*/*/index.json",)

_NOISE_DIRS = frozenset({"__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache"})
_NOISE_FILES = frozenset({".DS_Store"})
_NOISE_SUFFIXES = (".pyc", ".pyo")


class FrozenComponent(BaseModel):
    """One frozen path, hashed per file so a refusal can name what moved."""

    model_config = ConfigDict(extra="forbid")

    path: str
    digest: str
    # repo-relative POSIX path -> sha256 hex, sorted by path
    files: dict[str, str]


class FrozenFileChange(BaseModel):
    """One file that differs between a plan's frozen set and the tree."""

    model_config = ConfigDict(extra="forbid")

    component: str
    path: str
    change: Literal["changed", "added", "removed"]


class FrozenSetError(ValueError):
    """A path the plan has to freeze is missing, or the suite file names another suite."""


def suite_path(suite_id: str) -> str:
    return f"fixtures/suites/{suite_id}.json"


def file_sha256(path: Path) -> str:
    """sha256 of a file with CRLF folded to LF, so an autocrlf checkout hashes the same."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def component_digest(files: dict[str, str]) -> str:
    """One digest over a component's file hashes, independent of their order."""
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(f"{path}\0{files[path]}\n".encode())
    return f"sha256:{digest.hexdigest()}"


def _excluded(path: PurePosixPath, patterns: tuple[str, ...]) -> bool:
    return any(
        len(glob := PurePosixPath(pattern).parts) == len(path.parts)
        and all(map(fnmatchcase, path.parts, glob))
        for pattern in patterns
    )


def hash_component(root: Path, path: str, excluded: tuple[str, ...] = ()) -> FrozenComponent:
    """Hash a file or directory under ``root``. A missing path hashes as empty.

    ``excluded`` holds glob patterns matched one path segment at a time.
    """
    top = root / path
    found: list[str] = []
    if top.is_file():
        found.append(path)
    for directory, dirs, names in os.walk(top):
        here = Path(directory)
        rel = PurePosixPath(here.relative_to(root).as_posix())
        dirs[:] = [d for d in dirs if d not in _NOISE_DIRS and not _excluded(rel / d, excluded)]
        names = [
            name
            for name in names
            if name not in _NOISE_FILES
            and not name.endswith(_NOISE_SUFFIXES)
            and not _excluded(rel / name, excluded)
        ]
        found += [(rel / name).as_posix() for name in names]
    files = {rel: file_sha256(root / rel) for rel in sorted(found)}
    return FrozenComponent(path=path, digest=component_digest(files), files=files)


def compute_frozen_set(
    root: Path, *, suite_id: str, labels_path: str | None = None
) -> dict[str, FrozenComponent]:
    """Every component as the tree under ``root`` stands now."""
    frozen = {name: hash_component(root, path) for name, path in CODE_COMPONENTS.items()}
    frozen["suite"] = hash_component(root, suite_path(suite_id))
    frozen["fixtures"] = hash_component(root, FIXTURES_ROOT, FIXTURES_EXCLUDED)
    if labels_path is not None:
        frozen["labels"] = hash_component(root, PurePosixPath(labels_path).as_posix())
    return frozen


def freeze(
    root: Path, *, suite_id: str, labels_path: str | None = None
) -> dict[str, FrozenComponent]:
    """The frozen set for a new plan. Refuses paths that do not exist.

    Checking recomputes with :func:`compute_frozen_set`, where a missing path
    reads as every file removed. Freezing one would record an empty component
    that could never drift.
    """
    if labels_path is not None and PurePosixPath(labels_path).is_absolute():
        raise FrozenSetError(f"labels_path must be relative to the repository root: {labels_path}")
    required = [*CODE_COMPONENTS.values(), FIXTURES_ROOT, suite_path(suite_id)]
    for path in required + ([labels_path] if labels_path is not None else []):
        if not (root / path).exists():
            raise FrozenSetError(f"cannot freeze {path}: not found under {root.resolve()}")
    named = load_suite(root / suite_path(suite_id)).suite_id
    if named != suite_id:
        raise FrozenSetError(
            f"{suite_path(suite_id)} declares suite_id {named!r} where the plan names {suite_id!r}"
        )
    return compute_frozen_set(root, suite_id=suite_id, labels_path=labels_path)


def diff_frozen_sets(
    frozen: dict[str, FrozenComponent], current: dict[str, FrozenComponent]
) -> list[FrozenFileChange]:
    """Every file changed, added or removed, per component, in a stable order."""
    changes = []
    for name in sorted(frozen.keys() | current.keys()):
        before = frozen[name].files if name in frozen else {}
        after = current[name].files if name in current else {}
        for path in sorted(before.keys() | after.keys()):
            if path not in after:
                change = "removed"
            elif path not in before:
                change = "added"
            elif before[path] != after[path]:
                change = "changed"
            else:
                continue
            changes.append(FrozenFileChange(component=name, path=path, change=change))
    return changes


def check_frozen_set(
    frozen: dict[str, FrozenComponent],
    root: Path,
    *,
    suite_id: str,
    labels_path: str | None = None,
) -> list[FrozenFileChange]:
    """What differs between a plan's frozen set and the tree. Empty means unchanged."""
    current = compute_frozen_set(root, suite_id=suite_id, labels_path=labels_path)
    return diff_frozen_sets(frozen, current)


def render_changes(changes: list[FrozenFileChange]) -> list[str]:
    return [f"{c.component}: {c.change} {c.path}" for c in changes]
