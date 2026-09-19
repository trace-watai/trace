"""Docs may not quote a schema version the code disagrees with (#186).

Five versions in `docs/live_interface_compatibility_matrix.md` were one to
three bumps behind when this landed, and `docs/failure_bundles.md` described a
failure card two majors old. Nothing noticed, because a version in prose is
just a string. A reader trusting it would build against a contract that no
longer exists.

Docs quote versions as ``ModelName X.Y.Z``. That form is what makes this
checkable, so a doc that invents its own phrasing is invisible here and the
convention is worth keeping.
"""

from __future__ import annotations

import re

import pytest

from conftest import REPO_ROOT

DOCS_DIR = REPO_ROOT / "docs"

#: Documented type name -> the constant in ``src/`` that defines its version.
DOCUMENTED_VERSIONS = {
    "TaskSpec": "TASK_SCHEMA_VERSION",
    "State": "STATE_SCHEMA_VERSION",
    "TraceEvent": "TRACE_SCHEMA_VERSION",
    "RunConfig": "RUN_CONFIG_SCHEMA_VERSION",
    "RunResult": "RUN_RESULT_SCHEMA_VERSION",
    "RunIndex": "RUN_INDEX_SCHEMA_VERSION",
    "AttributionResult": "ATTRIBUTION_SCHEMA_VERSION",
    "FailureCard": "FAILURE_CARD_SCHEMA_VERSION",
    "RepairPackage": "REPAIR_PACKAGE_SCHEMA_VERSION",
    "RepairValidation": "REPAIR_VALIDATION_SCHEMA_VERSION",
    "RegressionArtifact": "REGRESSION_SCHEMA_VERSION",
    "Suite": "SUITE_SCHEMA_VERSION",
    "BatchSummary": "BATCH_SUMMARY_SCHEMA_VERSION",
    "SuiteReport": "SUITE_REPORT_SCHEMA_VERSION",
    "ControlLibrary": "CONTROL_LIBRARY_SCHEMA_VERSION",
    "ControlInstance": "CONTROL_SCHEMA_VERSION",
}

_CONSTANT_RE = re.compile(r'^([A-Z_]+SCHEMA_VERSION[A-Z_]*)\s*=\s*"([0-9.]+)"', re.M)
_QUOTED_RE = re.compile(r"\b([A-Z][A-Za-z]+)\s+`?([0-9]+\.[0-9]+\.[0-9]+)`?")

# Docs that describe a past state on purpose. A superseded review quoting the
# versions it reviewed is correct, and rewriting it would destroy the record.
HISTORICAL_DOCS = {
    "PROJECT_STATE_REVIEW_2026-07-28.md",
    "acceptance/2026-07-28-clean-checkout.md",
    "acceptance/refund-v0-suite.md",
    "acceptance/failure-bundles-v0.md",
}


def _constants() -> dict[str, str]:
    found: dict[str, str] = {}
    for path in (REPO_ROOT / "src").rglob("*.py"):
        for match in _CONSTANT_RE.finditer(path.read_text(encoding="utf-8")):
            found[match.group(1)] = match.group(2)
    return found


@pytest.fixture(scope="module")
def constants() -> dict[str, str]:
    return _constants()


def test_every_documented_name_has_a_constant(constants: dict[str, str]) -> None:
    """A name in the map with no constant means the map itself went stale."""
    missing = sorted(name for name, const in DOCUMENTED_VERSIONS.items() if const not in constants)
    assert not missing, f"documented names whose constant no longer exists: {missing}"


def test_no_doc_quotes_a_stale_schema_version(constants: dict[str, str]) -> None:
    stale = []
    for path in sorted(DOCS_DIR.rglob("*.md")):
        relative = str(path.relative_to(DOCS_DIR))
        if relative in HISTORICAL_DOCS:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for name, quoted in _QUOTED_RE.findall(line):
                constant = DOCUMENTED_VERSIONS.get(name)
                if constant is None:
                    continue
                current = constants[constant]
                if quoted != current:
                    stale.append(f"docs/{relative}:{number}: {name} {quoted}, code says {current}")
    assert not stale, "stale schema versions in docs:\n" + "\n".join(stale)


def test_the_check_would_catch_a_bumped_constant(constants: dict[str, str]) -> None:
    """The guard itself, proved rather than assumed.

    Bumping a constant without touching the docs must be caught, so pretend one
    moved and confirm the matrix line stops matching.
    """
    matrix = (DOCS_DIR / "live_interface_compatibility_matrix.md").read_text(encoding="utf-8")
    current = constants["TASK_SCHEMA_VERSION"]
    assert f"TaskSpec {current}" in matrix
    bumped = {**constants, "TASK_SCHEMA_VERSION": "9.9.9"}
    assert f"TaskSpec {bumped['TASK_SCHEMA_VERSION']}" not in matrix
