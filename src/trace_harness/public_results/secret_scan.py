"""Refuse to publish a tree that holds a credential.

    python -m trace_harness.public_results.secret_scan [PATH ...]

Scans every file under the given paths with :mod:`trace_harness.secret_scan`,
the one secret scanner for evidence (#198), and exits 1 on any hit. With no
paths it scans ``docs/acceptance/`` and every directory named ``cassettes`` in
the repository, which is what the publish job and a test in the gate run.

The shared scanner holds the key shapes, matches each line as written and
again with its JSON and percent escapes decoded, and also searches for the
values of the provider key variables when they are set. A hit names the file,
the line and the kind of secret and never the matched text, because CI logs on
a public repository are public. Exit codes: 0 clean, 1 a hit, 2 a path that
does not exist (a typo must not scan nothing and pass).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from trace_harness.secret_scan import files_under, key_values, scan_paths

DEFAULT_ROOT = "docs/acceptance"
CASSETTE_DIR_NAME = "cassettes"
_SKIP_DIRS = {".git", "node_modules", ".next", ".venv", "venv", "__pycache__"}


def default_targets(repo_root: Path) -> list[Path]:
    """``docs/acceptance/`` and every ``cassettes`` directory in the repository."""
    targets = [repo_root / DEFAULT_ROOT]
    for directory, children, _files in os.walk(repo_root):
        children[:] = sorted(c for c in children if c not in _SKIP_DIRS)
        here = Path(directory)
        if here.name == CASSETTE_DIR_NAME:
            targets.append(here)
            children[:] = []
    return targets


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    targets = [Path(a) for a in args] if args else default_targets(Path.cwd())
    try:
        scanned = len(files_under(targets))
        hits = scan_paths(targets, values=key_values())
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    where = ", ".join(str(t) for t in targets)
    if hits:
        for hit in hits:
            print(hit, file=sys.stderr)
        print(f"{len(hits)} secret(s) found in {scanned} file(s) under {where}", file=sys.stderr)
        return 1
    print(f"no secrets in {scanned} file(s) under {where}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
