"""Docs may not quote a schema version the code disagrees with (#186).

Six versions in `docs/live_interface_compatibility_matrix.md` were one to three
bumps behind when this landed, and `docs/failure_bundles.md` described a
failure card two versions old. Nothing noticed, because a version in prose is
just a string. A reader trusting it would build against a contract that no
longer exists. The first version of this test caught five of the six. It could
not read ``VERIFIER_RESULT_SCHEMA_VERSION``, whose value sits in parentheses,
so the constants are now read with ``ast`` rather than a regex.

Docs quote a version in one of three forms, and only these are checked:

- ``ModelName X.Y.Z``, for the names in ``DOCUMENTED_VERSIONS``;
- the constant itself, as ``TRACE_SCHEMA_VERSION = X.Y.Z``, ``... is X.Y.Z``
  or ``... (currently X.Y.Z``;
- a value followed by its constant, as ``"X.Y.Z"`` (``SUITE_REPORT_SCHEMA_VERSION``).

A doc that invents its own phrasing is invisible here, so the convention is
worth keeping.

Two kinds of quote are history on purpose and are skipped. Dated records
(acceptance records, ADRs, experiment briefs and pre-registrations, working
plans, the July 28 review) describe a moment, and rewriting them would destroy
the record. A quote right after "since", "from", "before" or "added in" names
the version a field arrived in, which stays true after later bumps.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from conftest import REPO_ROOT

DOCS_DIR = REPO_ROOT / "docs"
SRC_DIR = REPO_ROOT / "src"

#: Documented type name -> the constant in ``src/`` that defines its version.
DOCUMENTED_VERSIONS = {
    "TaskSpec": "TASK_SCHEMA_VERSION",
    "State": "STATE_SCHEMA_VERSION",
    "TraceEvent": "TRACE_SCHEMA_VERSION",
    "RunConfig": "RUN_CONFIG_SCHEMA_VERSION",
    "RunResult": "RUN_RESULT_SCHEMA_VERSION",
    "RunIndex": "RUN_INDEX_SCHEMA_VERSION",
    "VerifierInput": "VERIFIER_INPUT_SCHEMA_VERSION",
    "VerifierResult": "VERIFIER_RESULT_SCHEMA_VERSION",
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
    "MetricsSnapshot": "METRICS_SNAPSHOT_SCHEMA_VERSION",
    "FixtureScript": "FIXTURE_SCRIPT_SCHEMA_VERSION",
    "CassetteEntry": "CASSETTE_SCHEMA_VERSION",
}

#: Schema constants deliberately left out of ``DOCUMENTED_VERSIONS``, each
#: with the reason. A new constant in ``src/`` goes in one of the two, so a
#: schema can never be versioned without the docs check knowing about it.
EXEMPT_CONSTANTS: dict[str, str] = {}

#: Docs under these prefixes are dated records of a moment.
DATED_RECORD_PREFIXES = ("acceptance/", "decisions/", "experiments/", "superpowers/")
#: Single dated records that live beside current docs.
DATED_RECORDS = {"PROJECT_STATE_REVIEW_2026-07-28.md"}

_SCHEMA_CONSTANT = re.compile(r"[A-Z][A-Z0-9_]*SCHEMA_VERSION[A-Z0-9_]*")
_VERSION = r"([0-9]+\.[0-9]+(?:\.[0-9]+)?)\b"
_NAME_QUOTE_RE = re.compile(r"\b([A-Z][A-Za-z]+)`?\s+`?" + _VERSION)
_CONSTANT_QUOTE_RE = re.compile(
    r"\b(" + _SCHEMA_CONSTANT.pattern + r")\b`?\s*(?:=|is|\(currently)?\s*[`\"]*" + _VERSION
)
_VALUE_THEN_CONSTANT_RE = re.compile(
    r"[`\"]" + _VERSION + r"[`\"]*\s*\(`?(" + _SCHEMA_CONSTANT.pattern + r")\b"
)
_HISTORY_WORD_RE = re.compile(r"\b(?:since|from|before|added in)\s*[`\"(]*\s*$", re.IGNORECASE)


def schema_constants(source: str) -> dict[str, str]:
    """Module-level ``*SCHEMA_VERSION*`` string constants in one Python source.

    ``ast`` sees the value however it is written: parenthesized to fit a
    comment, annotated, or on its own line.
    """
    found: dict[str, str] = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
            continue
        for target in targets:
            if isinstance(target, ast.Name) and _SCHEMA_CONSTANT.fullmatch(target.id):
                found[target.id] = value.value
    return found


def unaccounted_constants(constants: dict[str, str]) -> list[str]:
    accounted = set(DOCUMENTED_VERSIONS.values()) | set(EXEMPT_CONSTANTS)
    return sorted(set(constants) - accounted)


def is_dated_record(relative: str) -> bool:
    return relative in DATED_RECORDS or relative.startswith(DATED_RECORD_PREFIXES)


def _is_history(line: str, start: int) -> bool:
    return _HISTORY_WORD_RE.search(line[:start]) is not None


def stale_quotes(text: str, constants: dict[str, str]) -> list[tuple[int, str, str, str]]:
    """Every quote in ``text`` that disagrees with ``constants``.

    Returns ``(line number, what was quoted, quoted version, current version)``.
    """
    stale = []
    for number, line in enumerate(text.splitlines(), 1):
        quotes = [
            (m.start(), DOCUMENTED_VERSIONS.get(m.group(1)), m.group(1), m.group(2))
            for m in _NAME_QUOTE_RE.finditer(line)
        ]
        quotes += [
            (m.start(), m.group(1), m.group(1), m.group(2))
            for m in _CONSTANT_QUOTE_RE.finditer(line)
        ]
        quotes += [
            (m.start(), m.group(2), m.group(2), m.group(1))
            for m in _VALUE_THEN_CONSTANT_RE.finditer(line)
        ]
        for start, constant, quoted_as, quoted in quotes:
            if constant is None or _is_history(line, start):
                continue
            current = constants.get(constant)
            if quoted != current:
                stale.append((number, quoted_as, quoted, str(current)))
    return stale


def stale_quotes_in_docs(docs_dir: Path, constants: dict[str, str]) -> list[str]:
    stale = []
    for path in sorted(docs_dir.rglob("*.md")):
        relative = path.relative_to(docs_dir).as_posix()
        if is_dated_record(relative):
            continue
        for number, quoted_as, quoted, current in stale_quotes(
            path.read_text(encoding="utf-8"), constants
        ):
            stale.append(f"docs/{relative}:{number}: {quoted_as} {quoted}, code says {current}")
    return stale


@pytest.fixture(scope="module")
def constants() -> dict[str, str]:
    found: dict[str, str] = {}
    for path in sorted(SRC_DIR.rglob("*.py")):
        found.update(schema_constants(path.read_text(encoding="utf-8")))
    return found


def test_every_documented_name_has_a_constant(constants: dict[str, str]) -> None:
    """A name in the map with no constant means the map itself went stale."""
    missing = sorted(name for name, const in DOCUMENTED_VERSIONS.items() if const not in constants)
    assert not missing, f"documented names whose constant no longer exists: {missing}"


def test_every_schema_constant_is_mapped_or_exempt(constants: dict[str, str]) -> None:
    """A constant the map does not know is a schema whose doc quotes go unchecked."""
    unaccounted = unaccounted_constants(constants)
    assert not unaccounted, (
        "schema constants missing from DOCUMENTED_VERSIONS and EXEMPT_CONSTANTS "
        f"in tests/test_docs_versions.py: {unaccounted}"
    )


def test_no_doc_quotes_a_stale_schema_version(constants: dict[str, str]) -> None:
    stale = stale_quotes_in_docs(DOCS_DIR, constants)
    assert not stale, "stale schema versions in docs:\n" + "\n".join(stale)


def test_the_trace_envelope_example_is_current(constants: dict[str, str]) -> None:
    """The envelope in trace_schema.md is a JSON example, which no quote form covers."""
    text = (DOCS_DIR / "trace_schema.md").read_text(encoding="utf-8")
    envelope = re.search(r'"schema_version":\s*"([0-9.]+)"', text)
    assert envelope is not None, "trace_schema.md lost its envelope example"
    assert envelope.group(1) == constants["TRACE_SCHEMA_VERSION"]


# The guard itself, proved on synthetic input rather than assumed.

_SYNTHETIC = {
    "TASK_SCHEMA_VERSION": "0.5.0",
    "TRACE_SCHEMA_VERSION": "0.5.0",
    "VERIFIER_RESULT_SCHEMA_VERSION": "0.4.0",
    "SUITE_REPORT_SCHEMA_VERSION": "0.1.0",
}


def test_the_reader_sees_a_parenthesized_or_annotated_constant() -> None:
    source = (
        "A_SCHEMA_VERSION = (\n"
        '    "0.4.0"  # a comment long enough to force the parentheses\n'
        ")\n"
        'B_SCHEMA_VERSION: str = "1.2.3"\n'
        "C_SCHEMA_VERSION = A_SCHEMA_VERSION\n"
        'lowercase_schema_version = "9.9.9"\n'
    )
    assert schema_constants(source) == {"A_SCHEMA_VERSION": "0.4.0", "B_SCHEMA_VERSION": "1.2.3"}


def test_a_new_constant_must_be_mapped_or_exempted() -> None:
    assert unaccounted_constants(
        {"TASK_SCHEMA_VERSION": "0.5.0", "NEW_SCHEMA_VERSION": "0.1.0"}
    ) == ["NEW_SCHEMA_VERSION"]


def test_each_quote_form_is_checked() -> None:
    text = (
        "Current: `TaskSpec 0.5.0` and `VerifierResult` 0.3.0.\n"
        "(`TRACE_SCHEMA_VERSION = 0.4.0`)\n"
        '| `schema_version` | str | `"0.0.9"` (`SUITE_REPORT_SCHEMA_VERSION`). |\n'
    )
    assert stale_quotes(text, _SYNTHETIC) == [
        (1, "VerifierResult", "0.3.0", "0.4.0"),
        (2, "TRACE_SCHEMA_VERSION", "0.4.0", "0.5.0"),
        (3, "SUITE_REPORT_SCHEMA_VERSION", "0.0.9", "0.1.0"),
    ]


def test_a_since_note_is_history() -> None:
    text = (
        "readable since TaskSpec 0.4.0, the default from `TraceEvent 0.4.0` on,\n"
        "absent before VerifierResult 0.3.0, and added in `TaskSpec` 0.2.0.\n"
    )
    assert stale_quotes(text, _SYNTHETIC) == []


def test_a_dated_record_is_skipped_and_a_current_doc_is_not(tmp_path: Path) -> None:
    dated = (
        "acceptance/run.md",
        "decisions/ADR-9.md",
        "experiments/briefs/9.md",
        "superpowers/plans/plan.md",
        "PROJECT_STATE_REVIEW_2026-07-28.md",
    )
    for relative in (*dated, "now.md"):
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_text("`TaskSpec 0.1.0`\n", encoding="utf-8")
    assert stale_quotes_in_docs(tmp_path, _SYNTHETIC) == [
        "docs/now.md:1: TaskSpec 0.1.0, code says 0.5.0"
    ]


def test_a_bumped_constant_is_caught_in_the_real_docs(constants: dict[str, str]) -> None:
    bumped = {**constants, "TASK_SCHEMA_VERSION": "9.9.9"}
    caught = stale_quotes_in_docs(DOCS_DIR, bumped)
    assert any(
        line.startswith("docs/live_interface_compatibility_matrix.md:") and "TaskSpec" in line
        for line in caught
    ), caught
