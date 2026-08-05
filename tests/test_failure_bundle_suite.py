"""TRA-40: the five complete failure bundles, produced by one suite run.

Executes fixtures/suites/refund_bundles_v0.json through the real batch
pipeline and pins the product claim: five distinct runnable failing cases —
each tripping a different verifier check — produce complete, schema-valid,
cross-consistent failure bundles with no manual assembly, while their four
positive siblings keep passing (no overblocking). The batch summary is the
machine-readable manifest of the five bundle run IDs.

Fully offline — the fixture provider needs no keys or network.
"""

from __future__ import annotations

import json

import pytest

from conftest import FIXTURES_DIR
from trace_harness.attribution.schemas import ATTRIBUTION_SCHEMA_VERSION, AttributionResult
from trace_harness.failure_bundles.schemas import (
    FAILURE_CARD_SCHEMA_VERSION,
    REPAIR_PACKAGE_SCHEMA_VERSION,
    FailureCard,
    RepairPackage,
)
from trace_harness.regression.schemas import REGRESSION_SCHEMA_VERSION, RegressionArtifact
from trace_harness.runner.batch import BatchRunner, BatchSummary, summary_path
from trace_harness.runner.suite import load_suite
from trace_harness.tracing.artifact_store import ArtifactStore
from trace_harness.verifiers.base import VERIFIER_RESULT_SCHEMA_VERSION, VerifierResult

SUITE_MANIFEST = FIXTURES_DIR / "suites" / "refund_bundles_v0.json"

# The five bundles and their pinned verifier signatures. Each failing case
# trips a DIFFERENT primary check — that distinctness is the point of the
# set, so it is asserted exactly, not as a subset.
EXPECTED_FAILURE_SIGNATURES: dict[str, set[str]] = {
    "refund_policy_failure": {
        "unauthorized_cash_refund",
        "ticket_outage_claim_unsupported",
        "deprecated_policy_treated_as_authoritative",
        "required_escalation_missing",
    },
    "refund_cash_age_boundary_day_31_no_approval": {"unauthorized_cash_refund"},
    "refund_outage_evidence_day_45_not_documented": {"unauthorized_store_credit"},
    "refund_policy_missing_info_failure": {"required_escalation_missing"},
    "refund_policy_phantom_refund": {"final_answer_inconsistent_with_state"},
}

EXPECTED_PRIMARY_CATEGORY: dict[str, str] = {
    "refund_policy_failure": "stale_source_authority",
    "refund_cash_age_boundary_day_31_no_approval": "unsafe_irreversible_action",
    "refund_outage_evidence_day_45_not_documented": "unsafe_irreversible_action",
    "refund_policy_missing_info_failure": "clarification_failure",
    "refund_policy_phantom_refund": "inconsistent_final_answer",
}

SIBLING_TASK_IDS = {
    "refund_policy_valid_cash",
    "refund_policy_store_credit",
    "refund_policy_no_refund",
    "refund_policy_missing_info",
}

BUNDLE_ARTIFACTS = (
    "verifier_result.json",
    "attribution_result.json",
    "failure_card.json",
    "repair_package.json",
    "regression_artifact.json",
)


@pytest.fixture(scope="module")
def bundle_batch(tmp_path_factory: pytest.TempPathFactory) -> tuple[ArtifactStore, BatchSummary]:
    """One suite execution shared by every assertion below (it is one product act)."""
    store = ArtifactStore(tmp_path_factory.mktemp("runs"))
    summary = BatchRunner(store).run(load_suite(SUITE_MANIFEST))
    return store, summary


def _load(store: ArtifactStore, run_id: str, name: str) -> dict:
    return json.loads((store.run_dir(run_id) / name).read_text(encoding="utf-8"))


def test_five_failing_cases_and_four_passing_siblings(
    bundle_batch: tuple[ArtifactStore, BatchSummary],
) -> None:
    _, summary = bundle_batch

    verdicts = {e.task_id: e.verifier_passed for e in summary.entries}
    assert verdicts == {
        **{task_id: False for task_id in EXPECTED_FAILURE_SIGNATURES},
        **{task_id: True for task_id in SIBLING_TASK_IDS},
    }
    agg = summary.aggregates
    assert agg.total == 9
    assert agg.completed == 9
    assert agg.terminated == 0
    assert agg.errored == 0
    assert agg.verifier_passed == 4
    assert agg.verifier_failed == 5


def test_each_bundle_trips_its_own_distinct_check(
    bundle_batch: tuple[ArtifactStore, BatchSummary],
) -> None:
    store, summary = bundle_batch

    for entry in summary.entries:
        if entry.task_id not in EXPECTED_FAILURE_SIGNATURES:
            continue
        assert entry.run_id is not None
        result = VerifierResult.model_validate(_load(store, entry.run_id, "verifier_result.json"))
        assert {c.check_id for c in result.failed_checks} == (
            EXPECTED_FAILURE_SIGNATURES[entry.task_id]
        ), entry.task_id

    # The four non-canonical bundles each isolate a different single check.
    single = [s for s in EXPECTED_FAILURE_SIGNATURES.values() if len(s) == 1]
    assert len(single) == 4
    assert len(set().union(*single)) == 4, "single-check bundles must not repeat a check"


def test_bundles_are_complete_and_cross_consistent(
    bundle_batch: tuple[ArtifactStore, BatchSummary],
) -> None:
    """Every failing run carries all five generated artifacts, mutually aligned
    by run ID and task ID, at the schema versions currently on main."""
    store, summary = bundle_batch

    for entry in summary.entries:
        if entry.task_id not in EXPECTED_FAILURE_SIGNATURES:
            continue
        assert entry.run_id is not None
        run_dir = store.run_dir(entry.run_id)
        for name in BUNDLE_ARTIFACTS:
            assert (run_dir / name).is_file(), f"{entry.task_id}: missing {name}"
        assert (run_dir / "trace.jsonl").is_file()

        verifier = VerifierResult.model_validate(_load(store, entry.run_id, "verifier_result.json"))
        attribution = AttributionResult.model_validate(
            _load(store, entry.run_id, "attribution_result.json")
        )
        card = FailureCard.model_validate(_load(store, entry.run_id, "failure_card.json"))
        repair = RepairPackage.model_validate(_load(store, entry.run_id, "repair_package.json"))
        regression = RegressionArtifact.model_validate(
            _load(store, entry.run_id, "regression_artifact.json")
        )

        # Linked by run ID and task ID (TRA-40 acceptance criterion).
        assert verifier.run_id == entry.run_id
        assert attribution.run_id == entry.run_id
        assert card.run_id == entry.run_id
        assert repair.run_id == entry.run_id
        assert regression.source_run_id == entry.run_id
        assert verifier.metadata["task_id"] == entry.task_id
        assert card.task_id == entry.task_id
        assert entry.task_id in regression.task_fixture

        # Contracts on main (versions asserted from the source constants so a
        # contract bump fails here loudly instead of drifting silently).
        assert verifier.schema_version == VERIFIER_RESULT_SCHEMA_VERSION
        assert attribution.schema_version == ATTRIBUTION_SCHEMA_VERSION
        assert card.schema_version == FAILURE_CARD_SCHEMA_VERSION
        assert repair.schema_version == REPAIR_PACKAGE_SCHEMA_VERSION
        assert regression.schema_version == REGRESSION_SCHEMA_VERSION

        # Evidence consistency: attribution matches the pinned category, and
        # no control is recommended without a matching failed verifier check.
        assert attribution.primary_failure_category == EXPECTED_PRIMARY_CATEGORY[entry.task_id]
        failed_ids = {c.check_id for c in verifier.failed_checks}
        for control in repair.controls:
            if control.name == "regression_test_ci_gate":
                continue  # the CI gate is always included by design
            assert set(control.linked_verifier_checks) & failed_ids, (
                f"{entry.task_id}: control {control.name} linked to no failed check"
            )
        # And every failed check is covered by at least one preventive control.
        covered = set().union(*(set(c.linked_verifier_checks) for c in repair.controls))
        assert failed_ids <= covered, f"{entry.task_id}: uncovered checks {failed_ids - covered}"

        # Rerunnable regression with an overblocking guard: every bundle pins
        # at least one positive sibling from the canonical suite.
        assert regression.positive_sibling_tests, f"{entry.task_id}: no positive sibling pinned"


def test_passing_siblings_produce_no_failure_artifacts(
    bundle_batch: tuple[ArtifactStore, BatchSummary],
) -> None:
    store, summary = bundle_batch

    for entry in summary.entries:
        if entry.task_id not in SIBLING_TASK_IDS:
            continue
        assert entry.run_id is not None
        run_dir = store.run_dir(entry.run_id)
        assert (run_dir / "verifier_result.json").is_file()
        for name in ("attribution_result.json", "failure_card.json", "repair_package.json"):
            assert not (run_dir / name).exists(), f"{entry.task_id}: unexpected {name}"


def test_batch_summary_is_the_bundle_manifest(
    bundle_batch: tuple[ArtifactStore, BatchSummary],
) -> None:
    """The persisted batch summary records all nine run IDs with verdicts —
    the machine-readable manifest TRA-40 requires."""
    store, summary = bundle_batch

    path = summary_path(store.runs_dir, summary.batch_id)
    assert path.is_file()
    persisted = json.loads(path.read_text(encoding="utf-8"))
    entries = {e["task_id"]: e for e in persisted["entries"]}
    assert set(entries) == set(EXPECTED_FAILURE_SIGNATURES) | SIBLING_TASK_IDS
    for task_id in EXPECTED_FAILURE_SIGNATURES:
        assert entries[task_id]["run_id"], f"{task_id}: manifest entry has no run_id"
        assert entries[task_id]["verifier_passed"] is False
