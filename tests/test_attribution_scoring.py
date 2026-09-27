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
from trace_harness.attribution.schemas import AttributionResult
from trace_harness.attribution.scoring import (
    AttributedRun,
    AttributionLabel,
    load_labels,
    score_attributions,
)
from trace_harness.cli import main
from trace_harness.runner.pipeline import run_task_pipeline
from trace_harness.runner.result import RunResult
from trace_harness.runner.suite import AgentConfig
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
            run_result = (
                RunResult.model_validate(store.read_json(run_id, names.RUN_RESULT))
                if store.exists(run_id, names.RUN_RESULT)
                else None
            )
            fresh = run_attribution(
                DEFAULT_METHOD,
                TaskSpec.model_validate(store.read_json(run_id, names.TASK_SPEC)),
                store.read_trace(run_id),
                VerifierResult.model_validate(store.read_json(run_id, names.VERIFIER_RESULT)),
                run_result,
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


class _Costed:
    """A method that reports its own cost, or none, the two ways the protocol allows."""

    deterministic = False
    last_cost_usd: float | None = None

    def __init__(self, name: str, metadata_cost: float | None) -> None:
        self.name = name
        self.metadata_cost = metadata_cost

    def attribute(self, task, trace, verifier_result, run_result=None) -> AttributionResult:
        result = get_attribution_method(DEFAULT_METHOD).attribute(
            task, trace, verifier_result, run_result
        )
        if self.metadata_cost is None:
            return result
        return result.model_copy(update={"metadata": {COST_KEY: self.metadata_cost}})


@pytest.mark.parametrize(("metadata_cost", "stamped"), [(0.12, 0.12), (None, None)])
def test_a_methods_own_cost_is_kept_and_an_unreported_one_is_unknown(
    monkeypatch, metadata_cost, stamped
) -> None:
    from trace_harness.attribution import registry

    monkeypatch.setitem(registry._METHODS, "costed", _Costed("costed", metadata_cost))
    store = ArtifactStore(REPO_ROOT / "docs" / "acceptance" / "runs")
    (run_id,) = [r for r in store.list_runs() if store.exists(r, names.ATTRIBUTION_RESULT)]
    result = run_attribution(
        "costed",
        TaskSpec.model_validate(store.read_json(run_id, names.TASK_SPEC)),
        store.read_trace(run_id),
        VerifierResult.model_validate(store.read_json(run_id, names.VERIFIER_RESULT)),
    )
    assert result.metadata[COST_KEY] == stamped


def test_the_pipeline_hands_the_run_result_to_the_method(tmp_path, monkeypatch) -> None:
    """The post-block label reads how the run ended, so it must reach the method."""
    from trace_harness.attribution import registry

    seen: list[RunResult | None] = []
    heuristic = registry._METHODS[DEFAULT_METHOD]

    class _Recording:
        name = DEFAULT_METHOD
        deterministic = True
        last_cost_usd = 0.0

        def attribute(self, task, trace, verifier_result, run_result=None):
            seen.append(run_result)
            return heuristic.attribute(task, trace, verifier_result, run_result)

    monkeypatch.setitem(registry._METHODS, DEFAULT_METHOD, _Recording())
    failure = FIXTURES_DIR / "tasks" / "refund_policy_failure.json"
    result = run_task_pipeline(failure, AgentConfig(label="t"), ArtifactStore(tmp_path / "runs"))
    assert [r.run_id if r else None for r in seen] == [result.run_result.run_id]


# --- the labels ---


def test_every_label_carries_provenance_for_each_field() -> None:
    """A label nobody can trace back to a line of the script is not ground truth."""
    for label in load_labels(GROUND_TRUTH):
        for field in (
            "root_cause_step",
            "missed_recovery_step",
            "first_irreversible_action_step",
            "primary_failure_category",
        ):
            assert label.labels(field), f"{label.key} leaves {field} out"
            assert label.provenance.get(field), f"{label.key} has no provenance for {field}"


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ('{"task_id": "t", "root_cause_step": "3"}', "root_cause_step"),
        ('{"task_id": "t", "root_cuase_step": 3}', "root_cuase_step"),
        ('{"root_cause_step": 3}', "run_id or task_id"),
        ('{"task_id": "t", "primary_failure_category": "vibes"}', "primary_failure_category"),
    ],
)
def test_a_malformed_label_is_refused_with_its_line(tmp_path, capsys, line, message) -> None:
    labels = tmp_path / "labels.jsonl"
    labels.write_text('{"task_id": "ok"}\n' + line + "\n", encoding="utf-8")
    runs = str(tmp_path / "runs")
    assert main(["--runs-dir", runs, "score-attribution", "--labels", str(labels)]) == 2
    err = capsys.readouterr().err
    assert f"{labels}:2" in err and message in err


def test_an_unknown_method_is_refused_before_anything_runs(tmp_path, capsys) -> None:
    runs = str(tmp_path / "runs")
    argv = ["--runs-dir", runs, "score-attribution", "--labels", str(GROUND_TRUTH)]
    assert main([*argv, "--method", "psychic"]) == 2
    assert "available" in capsys.readouterr().err
    assert not (tmp_path / "runs" / "attribution_score.json").exists()


# --- scoring ---


def _label(**fields) -> AttributionLabel:
    return AttributionLabel.model_validate({"run_id": "r", **fields})


def _score(labels: list[AttributionLabel], *runs: AttributedRun):
    return score_attributions(
        method="heuristic", labels_path=Path("x.jsonl"), labels=labels, runs=list(runs)
    )


def _run(run_id: str = "r", task_id: str = "t", **attribution) -> AttributedRun:
    return AttributedRun(run_id, task_id, attribution)


def test_null_is_a_real_label_not_a_near_miss() -> None:
    """Inventing a missed recovery where there is none is wrong, not off by one."""
    field = _score([_label(missed_recovery_step=None)], _run(missed_recovery_step=4)).step_fields[
        "missed_recovery_step"
    ]
    assert (field.labeled, field.exact, field.off_by_one, field.declined) == (1, 0, 0, 0)


def test_a_declined_step_is_undefined_under_c1() -> None:
    """A method that names no step where the label names one leaves the denominator."""
    score = _score([_label(root_cause_step=3)], _run(root_cause_step=None))
    field = score.step_fields["root_cause_step"]
    assert (field.labeled, field.exact, field.declined) == (0, 0, 1)
    assert field.exact_accuracy is None and field.off_by_one_accuracy is None


def test_a_field_nobody_labeled_has_no_accuracy() -> None:
    score = _score([_label(root_cause_step=3)], _run(root_cause_step=3))
    unlabeled = score.step_fields["first_irreversible_action_step"]
    assert (unlabeled.labeled, unlabeled.exact_accuracy) == (0, None)
    assert score.category.exact_accuracy is None


def test_off_by_one_counts_a_neighbouring_step() -> None:
    field = _score([_label(root_cause_step=3)], _run(root_cause_step=4)).step_fields[
        "root_cause_step"
    ]
    assert (field.exact, field.off_by_one) == (0, 1)


def test_a_label_with_no_attribution_is_reported_not_counted() -> None:
    labels = [_label(root_cause_step=1), _label(run_id="absent", root_cause_step=1)]
    score = _score(labels, _run(root_cause_step=1))
    assert score.scored_labels == 1
    assert score.unscored_labels == ["run_id=absent"]


def test_labels_keyed_by_run_score_each_run_and_each_labeler() -> None:
    """Two runs of one task each get their own label, and two labelers both count."""
    labels = [
        _label(run_id="r1", root_cause_step=3, labeler="a"),
        _label(run_id="r1", root_cause_step=4, labeler="b"),
        _label(run_id="r2", root_cause_step=5),
    ]
    score = _score(labels, _run("r1", root_cause_step=3), _run("r2", root_cause_step=5))
    field = score.step_fields["root_cause_step"]
    assert (score.scored_labels, score.scored_runs, field.labeled, field.exact) == (3, 2, 3, 2)


def test_a_task_key_with_several_runs_is_ambiguous_not_guessed() -> None:
    label = AttributionLabel.model_validate({"task_id": "t", "root_cause_step": 3})
    score = _score([label], _run("r1", root_cause_step=3), _run("r2", root_cause_step=9))
    assert score.scored_labels == 0
    assert score.ambiguous_labels == ["task_id=t"]


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
    """#31's labels are keyed by run id, one row per labeler; two rows prove the shape."""
    runs = tmp_path / "runs"
    assert main(["--runs-dir", str(runs), "run-suite", str(BUNDLES)]) == 0
    store = ArtifactStore(runs)
    run_of = {
        store.read_json(run_id, names.TASK_SPEC)["task_id"]: run_id
        for run_id in store.list_runs()
        if store.exists(run_id, names.TASK_SPEC)
    }
    labels = tmp_path / "human_labels_sample.jsonl"
    rows = [
        {
            "run_id": run_of["refund_policy_failure"],
            "root_cause_step": 3,
            "primary_failure_category": "stale_source_authority",
            "labeler": "human-a",
        },
        {
            "run_id": run_of["refund_policy_phantom_refund"],
            "root_cause_step": 3,
            "primary_failure_category": "inconsistent_final_answer",
            "labeler": "human-b",
        },
    ]
    labels.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    assert main(["--runs-dir", str(runs), "score-attribution", "--labels", str(labels)]) == 0

    score = json.loads((runs / "attribution_score.json").read_text(encoding="utf-8"))
    assert (score["scored_labels"], score["scored_runs"]) == (2, 2)
    assert score["category"]["exact_accuracy"] == 1.0
    assert score["step_fields"]["root_cause_step"]["exact"] == 2
