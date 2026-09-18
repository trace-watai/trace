"""The attribution method seam and its pinned score (#189).

The pin is the point. "A judge must beat the heuristic" is only checkable if
the heuristic's number is written down, so a detector change that moves it
fails here until the pin is updated in the same PR.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import FIXTURES_DIR, REPO_ROOT
from trace_harness.attribution.base import (
    COST_KEY,
    DETERMINISTIC_KEY,
    METHOD_NAME_KEY,
    AttributionMethod,
)
from trace_harness.attribution.registry import (
    DEFAULT_METHOD,
    UnknownAttributionMethodError,
    available_methods,
    get_attribution_method,
    run_attribution,
)
from trace_harness.attribution.scoring import load_labels, score_attributions
from trace_harness.cli import main
from trace_harness.tasks.schemas import TaskSpec
from trace_harness.tracing import artifact_store as names
from trace_harness.tracing.artifact_store import ArtifactStore
from trace_harness.verifiers.base import VerifierResult

GROUND_TRUTH = FIXTURES_DIR / "attribution_ground_truth" / "refund_v0_staged.jsonl"
PINNED_SCORE = FIXTURES_DIR / "expected" / "heuristic_attribution_score.json"
BUNDLES = FIXTURES_DIR / "suites" / "refund_bundles_v0.json"


# --- the seam ---


def test_heuristic_is_registered_and_satisfies_the_protocol() -> None:
    method = get_attribution_method(DEFAULT_METHOD)
    assert isinstance(method, AttributionMethod)
    assert method.deterministic is True
    assert DEFAULT_METHOD in available_methods()


def test_unknown_method_names_the_options() -> None:
    with pytest.raises(UnknownAttributionMethodError, match="available"):
        get_attribution_method("psychic")


def test_registry_output_matches_the_committed_attributions() -> None:
    """Going through the registry must not change what the heuristic decides."""
    for root in ("runs", "live-gemini-2026-09-13"):
        store = ArtifactStore(REPO_ROOT / "docs" / "acceptance" / root)
        for run_id in store.list_runs():
            if not store.exists(run_id, names.ATTRIBUTION_RESULT):
                continue
            pinned = store.read_json(run_id, names.ATTRIBUTION_RESULT)
            fresh = run_attribution(
                DEFAULT_METHOD,
                TaskSpec.model_validate(store.read_json(run_id, names.TASK_SPEC)),
                store.read_trace(run_id),
                VerifierResult.model_validate(store.read_json(run_id, names.VERIFIER_RESULT)),
            ).model_dump(mode="json")
            differing = {
                key
                for key in pinned
                if key not in ("schema_version", "metadata") and pinned[key] != fresh.get(key)
            }
            assert not differing, f"{run_id} changed on {sorted(differing)}"


def test_method_metadata_is_stamped() -> None:
    store = ArtifactStore(REPO_ROOT / "docs" / "acceptance" / "runs")
    (run_id,) = [r for r in store.list_runs() if store.exists(r, names.ATTRIBUTION_RESULT)]
    result = run_attribution(
        DEFAULT_METHOD,
        TaskSpec.model_validate(store.read_json(run_id, names.TASK_SPEC)),
        store.read_trace(run_id),
        VerifierResult.model_validate(store.read_json(run_id, names.VERIFIER_RESULT)),
    )

    assert result.metadata[METHOD_NAME_KEY] == DEFAULT_METHOD
    assert result.metadata[COST_KEY] == 0.0
    assert result.metadata[DETERMINISTIC_KEY] is True


# --- the labels ---


def test_every_label_carries_provenance_for_each_field() -> None:
    """A label nobody can trace back to a line of the script is not ground truth."""
    for row in load_labels(GROUND_TRUTH):
        provenance = row.get("provenance", {})
        for field in (
            "root_cause_step",
            "missed_recovery_step",
            "first_irreversible_action_step",
            "primary_failure_category",
        ):
            assert provenance.get(field), f"{row['task_id']} has no provenance for {field}"


# --- scoring ---


def test_null_is_a_real_label_not_a_near_miss() -> None:
    """Inventing a missed recovery where there is none is wrong, not off by one."""
    labels = [{"task_id": "t", "missed_recovery_step": None}]
    score = score_attributions(
        method="heuristic",
        labels_path=Path("x.jsonl"),
        labels=labels,
        attributions={"t": {"missed_recovery_step": 4}},
    )
    field = score.step_fields["missed_recovery_step"]
    assert field.exact == 0
    assert field.off_by_one == 0


def test_off_by_one_counts_a_neighbouring_step() -> None:
    score = score_attributions(
        method="heuristic",
        labels_path=Path("x.jsonl"),
        labels=[{"task_id": "t", "root_cause_step": 3}],
        attributions={"t": {"root_cause_step": 4}},
    )
    assert score.step_fields["root_cause_step"].exact == 0
    assert score.step_fields["root_cause_step"].off_by_one == 1


def test_a_label_with_no_attribution_is_reported_not_counted() -> None:
    score = score_attributions(
        method="heuristic",
        labels_path=Path("x.jsonl"),
        labels=[{"task_id": "present", "root_cause_step": 1}, {"task_id": "absent"}],
        attributions={"present": {"root_cause_step": 1}},
    )
    assert score.labeled_tasks == 1
    assert score.unscored_tasks == ["absent"]


def test_heuristic_score_matches_the_pin(tmp_path: Path) -> None:
    """Change a detector and this fails until the pin moves in the same PR."""
    runs = tmp_path / "runs"
    assert main(["--runs-dir", str(runs), "run-suite", str(BUNDLES)]) == 0
    assert (
        main(
            [
                "--runs-dir",
                str(runs),
                "score-attribution",
                "--labels",
                str(GROUND_TRUTH),
            ]
        )
        == 0
    )

    produced = json.loads((runs / "attribution_score.json").read_text(encoding="utf-8"))
    expected = json.loads(PINNED_SCORE.read_text(encoding="utf-8"))
    # labels_path is absolute in the run and relative in the pin.
    produced.pop("labels_path")
    expected.pop("labels_path")
    assert produced == expected


def test_a_human_label_file_scores_with_no_code_change(tmp_path: Path) -> None:
    """#31's labels are the same shape; two records are enough to prove it."""
    labels = tmp_path / "human_labels_sample.jsonl"
    labels.write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {
                    "task_id": "refund_policy_failure",
                    "root_cause_step": 3,
                    "primary_failure_category": "stale_source_authority",
                    "labeler": "human-a",
                },
                {
                    "task_id": "refund_policy_phantom_refund",
                    "root_cause_step": 3,
                    "primary_failure_category": "inconsistent_final_answer",
                    "labeler": "human-b",
                },
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    runs = tmp_path / "runs"
    assert main(["--runs-dir", str(runs), "run-suite", str(BUNDLES)]) == 0
    assert main(["--runs-dir", str(runs), "score-attribution", "--labels", str(labels)]) == 0

    score = json.loads((runs / "attribution_score.json").read_text(encoding="utf-8"))
    assert score["labeled_tasks"] == 2
    assert score["category_accuracy"] == 1.0
    assert score["step_fields"]["root_cause_step"]["exact"] == 2
