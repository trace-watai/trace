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
``labels``       the plan's ``labels_path``, a file under the repository root,
                 when it names one.

A file hash is sha256 over the file's bytes with CRLF folded to LF, keyed by
its repo-relative POSIX path. A component digest is sha256 over the sorted
``path, hash`` list. Neither a checkout's line endings nor the order a
filesystem lists a directory changes a digest. ``__pycache__``, ``.pyc``,
``.pyo``, the mypy, pytest and ruff caches and ``.DS_Store`` are skipped.
Every other file counts, an editor's swap or backup file included. A symlink
inside a component is refused, because ``os.walk`` does not descend a linked
directory and its files would drop out of the hash without a word.
"""

from __future__ import annotations

import hashlib
import os
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

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

    @model_validator(mode="after")
    def _digest_covers_the_files(self) -> FrozenComponent:
        # Drift is found from ``files`` and fixtures_hash is checked against
        # ``digest``, so the two have to describe the same hashes.
        if self.digest != component_digest(self.files):
            raise ValueError(f"the {self.path} digest {self.digest} does not match its files")
        return self


class FrozenFileChange(BaseModel):
    """One file that differs between a plan's frozen set and the tree."""

    model_config = ConfigDict(extra="forbid")

    component: str
    path: str
    change: Literal["changed", "added", "removed"]


class FrozenSetError(ValueError):
    """The frozen set cannot be hashed from this tree, or the plan names a bad path."""


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


def check_labels_path(labels_path: str) -> str:
    """``labels_path`` when it names a path under the repository root.

    Relative, with ``/`` separators and no empty, ``.`` or ``..`` segment. An
    empty path, ``.`` or ``..`` would freeze the whole tree or its parent as
    the labels.
    """
    if (
        not labels_path
        or "\\" in labels_path
        or PureWindowsPath(labels_path).anchor
        or any(part in ("", ".", "..") for part in labels_path.split("/"))
    ):
        raise FrozenSetError(
            "labels_path must be a relative POSIX path under the repository root, with no "
            f"empty, '.' or '..' segment: {labels_path!r}"
        )
    return labels_path


def _excluded(path: PurePosixPath, patterns: tuple[str, ...]) -> bool:
    return any(
        len(glob := PurePosixPath(pattern).parts) == len(path.parts)
        and all(map(fnmatchcase, path.parts, glob))
        for pattern in patterns
    )


def _refuse_symlink(root: Path, candidate: Path) -> None:
    if candidate.is_symlink():
        rel = candidate.relative_to(root).as_posix()
        raise FrozenSetError(
            f"{rel} is a symlink; the frozen set hashes regular files only, so replace it "
            "with the files it points to or move it out of the frozen paths"
        )


def hash_component(root: Path, path: str, excluded: tuple[str, ...] = ()) -> FrozenComponent:
    """Hash a file or directory under ``root``. A missing path hashes as empty.

    ``excluded`` holds glob patterns matched one path segment at a time. A path
    that resolves outside ``root``, or a symlink anywhere in the component, is
    a :class:`FrozenSetError`.
    """
    top = root / path
    if not top.resolve().is_relative_to(root.resolve()):
        raise FrozenSetError(f"cannot hash {path}: it resolves outside {root.resolve()}")
    _refuse_symlink(root, top)
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
        for name in [*dirs, *names]:
            _refuse_symlink(root, here / name)
        found += [(rel / name).as_posix() for name in names]
    files = {rel: file_sha256(root / rel) for rel in sorted(found)}
    return FrozenComponent(path=path, digest=component_digest(files), files=files)


def compute_frozen_set(
    root: Path, *, suite_id: str, labels_path: str | None = None
) -> dict[str, FrozenComponent]:
    """Every component as the tree under ``root`` stands now.

    A ``root`` holding none of the code components is refused. It is almost
    always the wrong working directory, and hashing it would report every
    frozen file as removed.
    """
    if not any((root / path).is_dir() for path in CODE_COMPONENTS.values()):
        raise FrozenSetError(
            f"none of {', '.join(CODE_COMPONENTS.values())} exists under {root.resolve()}; "
            "the frozen set is hashed from the working directory, so run from the "
            "repository root"
        )
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

    A missing path is most often a typo or the wrong working directory.
    Freezing it would record an empty component, which pins nothing that
    exists at freeze time. The labels must be one file under ``root``.
    """
    if labels_path is not None:
        check_labels_path(labels_path)
    required = [*CODE_COMPONENTS.values(), FIXTURES_ROOT, suite_path(suite_id)]
    for path in required + ([labels_path] if labels_path is not None else []):
        if not (root / path).exists():
            raise FrozenSetError(f"cannot freeze {path}: not found under {root.resolve()}")
    if labels_path is not None and not (root / labels_path).is_file():
        raise FrozenSetError(f"labels_path must name a file: {labels_path} is a directory")
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
