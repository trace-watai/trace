"""#211: the bundle key a failure card is deduplicated on, and the 0.5.0 card.

A key is formed from the failed check set, the primary failure category and the
tool at the first irreversible step, and from nothing else. These tests pin the
key of every failing task fixture, the recipe, what the key ignores and what
changes it, and that cards and index entries written before the bump still load.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from conftest import FAILURE_TASK_PATH, FIXTURES_DIR, REPO_ROOT
from trace_harness.attribution.schemas import AttributionResult, FailureCategory
from trace_harness.failure_bundles.generator import bundle_key, first_irreversible_tool
from trace_harness.failure_bundles.schemas import FailureCard
from trace_harness.runner.pipeline import run_task_pipeline
from trace_harness.runner.suite import AgentConfig
from trace_harness.tracing import artifact_store as names
from trace_harness.tracing.artifact_store import ArtifactStore
from trace_harness.tracing.run_index import RUN_INDEX_SCHEMA_VERSION, RunIndex
from trace_harness.verifiers.base import VerifierResult

TASKS_DIR = FIXTURES_DIR / "tasks"

# The key of every failing task fixture, pinned. The key is the identity that
# decides which runs share a card, so a change to how it is formed has to show
# up here as a deliberate edit rather than as cards that silently stop
# merging. Nineteen failing fixtures form fourteen keys. Four groups of
# different tasks, nine fixtures in all, fail the same way and would share a
# card in one runs directory. Neither pinned suite runs two tasks from one
# group, which test_no_pinned_suite_runs_two_tasks_with_one_key asserts, so
# every pinned suite expectation keeps one card per failing task.
PINNED_KEYS = {
    "refund_policy_failure": "v1:stale_source_authority:issue_refund:5d5539fd98990b68",
    "refund_policy_control_demo": "v1:unsafe_irreversible_action:issue_refund:e621a26e69c17400",
    "refund_cash_age_boundary_day_31_no_approval": (
        "v1:unsafe_irreversible_action:issue_refund:e621a26e69c17400"
    ),
    "refund_cash_age_boundary_day_61_violation": (
        "v1:unsafe_irreversible_action:issue_refund:e621a26e69c17400"
    ),
    "refund_outage_evidence_day_45_credit_violation": (
        "v1:unsafe_irreversible_action:issue_refund:88e370f122f88008"
    ),
    "refund_outage_evidence_day_45_not_documented": (
        "v1:unsafe_irreversible_action:issue_refund:88e370f122f88008"
    ),
    "refund_policy_missing_info_failure": "v1:clarification_failure:none:10f9e382dafdffbf",
    "refund_escalation_missing": "v1:clarification_failure:none:10f9e382dafdffbf",
    "refund_policy_phantom_refund": "v1:inconsistent_final_answer:none:0df089a5ff187028",
    "refund_final_answer_phantom": "v1:inconsistent_final_answer:none:0df089a5ff187028",
    "refund_final_answer_denied_real": (
        "v1:inconsistent_final_answer:issue_refund:d8a148cc38185d68"
    ),
    "refund_retrieval_missed_current": "v1:unknown:issue_refund:4ea80fb65f93ec02",
    "refund_expected_action_cash_swapped": "v1:unknown:issue_refund:50b1c713c275620c",
    "refund_retrieval_skipped": "v1:unknown:issue_refund:86de785155f52d96",
    "refund_retrieval_decline_ungrounded": "v1:unknown:none:49816a5d7bfa0978",
    "refund_expected_action_decline_escalated": "v1:unknown:none:72960ccabb7b3742",
    "refund_escalation_unnecessary": "v1:unknown:none:888b753f070db380",
    "refund_expected_action_cash_omitted": "v1:unknown:none:c7b4ba7570e89c7e",
    "refund_escalation_duplicate": "v1:unknown:none:fc58d3cdeade159d",
}


def _task_path(task_id: str) -> Path:
    (path,) = TASKS_DIR.rglob(f"{task_id}.json")
    return path


def _pipeline(task: Path, store: ArtifactStore, **config) -> str:
    agent = AgentConfig(label=config.pop("label", "fixture"), **config)
    return run_task_pipeline(task, agent, store).run_result.run_id


def _card(store: ArtifactStore, run_id: str) -> FailureCard:
    return FailureCard.model_validate(store.read_json(run_id, names.FAILURE_CARD))


def _key_inputs(store: ArtifactStore, run_id: str):
    return (
        VerifierResult.model_validate(store.read_json(run_id, names.VERIFIER_RESULT)),
        AttributionResult.model_validate(store.read_json(run_id, names.ATTRIBUTION_RESULT)),
        store.read_trace(run_id),
    )


# --- the key ------------------------------------------------------------


@pytest.mark.parametrize("task_id", sorted(PINNED_KEYS))
def test_every_failing_fixture_bundles_under_its_pinned_key(tmp_path, task_id):
    store = ArtifactStore(tmp_path / "runs")
    run_id = _pipeline(_task_path(task_id), store)
    assert _card(store, run_id).bundle_key == PINNED_KEYS[task_id]


def test_the_pinned_keys_cover_every_failing_fixture(tmp_path):
    """A new failing fixture has to be pinned here, so its key is a decision too."""
    failing = set()
    for path in sorted(TASKS_DIR.rglob("*.json")):
        if "counterexamples" in path.parts:
            continue
        store = ArtifactStore(tmp_path / path.stem)
        result = run_task_pipeline(path, AgentConfig(label="fixture"), store)
        if result.verifier_result is not None and result.verifier_result.has_violations:
            failing.add(result.task.task_id)
    assert failing == set(PINNED_KEYS)
    assert len(set(PINNED_KEYS.values())) == 14


@pytest.mark.parametrize("suite", ["refund_v0.json", "refund_bundles_v0.json"])
def test_no_pinned_suite_runs_two_tasks_with_one_key(suite):
    tasks = json.loads((FIXTURES_DIR / "suites" / suite).read_text())["tasks"]
    keys = [PINNED_KEYS[Path(t).stem] for t in tasks if Path(t).stem in PINNED_KEYS]
    assert keys and len(keys) == len(set(keys))


def test_the_key_is_the_documented_recipe(tmp_path):
    store = ArtifactStore(tmp_path / "runs")
    run_id = _pipeline(FAILURE_TASK_PATH, store)
    verifier, _, _ = _key_inputs(store, run_id)
    canonical = json.dumps(
        {
            "version": "v1",
            "checks": sorted({c.check_id for c in verifier.failed_checks}),
            "category": "stale_source_authority",
            "tool": "issue_refund",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    assert _card(store, run_id).bundle_key == f"v1:stale_source_authority:issue_refund:{digest}"


def test_the_key_ignores_order_repeats_and_everything_but_its_three_facts(tmp_path):
    store = ArtifactStore(tmp_path / "runs")
    run_id = _pipeline(FAILURE_TASK_PATH, store)
    verifier, attribution, trace = _key_inputs(store, run_id)
    key = bundle_key(verifier, attribution, trace)

    checks = verifier.failed_checks
    reworded = [c.model_copy(update={"step_ids": [99], "message": "reworded"}) for c in checks]
    shuffled = verifier.model_copy(
        update={"failed_checks": [*reversed(reworded), checks[0]], "run_id": "run_other"}
    )
    elsewhere = attribution.model_copy(
        update={"run_id": "run_other", "root_cause_step": 1, "confidence": 0.1}
    )
    assert bundle_key(shuffled, elsewhere, trace) == key


def test_each_of_the_three_facts_changes_the_key(tmp_path):
    store = ArtifactStore(tmp_path / "runs")
    run_id = _pipeline(FAILURE_TASK_PATH, store)
    verifier, attribution, trace = _key_inputs(store, run_id)
    key = bundle_key(verifier, attribution, trace)

    fewer_checks = verifier.model_copy(update={"failed_checks": verifier.failed_checks[1:]})
    other_category = attribution.model_copy(
        update={"primary_failure_category": FailureCategory.POLICY_VIOLATION}
    )
    no_irreversible = attribution.model_copy(update={"first_irreversible_action_step": None})
    variants = {
        bundle_key(fewer_checks, attribution, trace),
        bundle_key(verifier, other_category, trace),
        bundle_key(verifier, no_irreversible, trace),
    }
    assert key not in variants and len(variants) == 3
    assert bundle_key(verifier, no_irreversible, trace).split(":")[2] == "none"


def test_the_tool_is_read_at_the_attributions_first_irreversible_step(tmp_path):
    store = ArtifactStore(tmp_path / "runs")
    run_id = _pipeline(FAILURE_TASK_PATH, store)
    _, attribution, trace = _key_inputs(store, run_id)

    assert attribution.first_irreversible_action_step == 5
    assert first_irreversible_tool(trace, attribution) == "issue_refund"
    # Step 3 executed no irreversible tool, so nothing is read off it.
    moved = attribution.model_copy(update={"first_irreversible_action_step": 3})
    assert first_irreversible_tool(trace, moved) is None


# --- the 0.5.0 card and files written before it -------------------------


def test_a_card_lists_each_run_once_starting_with_its_own(tmp_path):
    store = ArtifactStore(tmp_path / "runs")
    first = _pipeline(FAILURE_TASK_PATH, store)
    data = store.read_json(first, names.FAILURE_CARD)
    own = data["occurrences"][0]

    for occurrences in ([own, own], [{**own, "run_id": "run_other"}, own]):
        with pytest.raises(ValueError):
            FailureCard.model_validate({**data, "occurrences": occurrences})


def _as_written_before_keys(store: ArtifactStore, run_id: str) -> None:
    data = store.read_json(run_id, names.FAILURE_CARD)
    data["schema_version"] = "0.4.0"
    del data["bundle_key"], data["occurrences"]
    store.write_json(run_id, names.FAILURE_CARD, data)


def test_a_card_written_before_keys_loads_and_describes_its_own_run(tmp_path):
    store = ArtifactStore(tmp_path / "runs")
    run_id = _pipeline(FAILURE_TASK_PATH, store)
    _as_written_before_keys(store, run_id)

    card = _card(store, run_id)
    assert (card.schema_version, card.bundle_key, card.occurrences) == ("0.4.0", None, [])
    # The index still holds the key the bundle stage wrote. A rebuild reads the
    # key off the card, and a card from before 0.5.0 has none.
    assert store.read_index().entries[0].bundle_key is not None
    (entry,) = store.rebuild_index().entries
    assert entry.bundle_key is None


def test_an_index_written_before_keys_is_rebuilt_with_them(tmp_path):
    store = ArtifactStore(tmp_path / "runs")
    first = _pipeline(FAILURE_TASK_PATH, store)
    second = _pipeline(FAILURE_TASK_PATH, store)
    key = _card(store, first).bundle_key
    old = json.loads(store.index_path().read_text())
    old["schema_version"] = "0.5.0"
    for entry in old["entries"]:
        del entry["bundle_key"]
    store.index_path().write_text(json.dumps(old))

    index = store.read_index()

    assert index.schema_version == RUN_INDEX_SCHEMA_VERSION == "0.6.0"
    assert {e.run_id: e.bundle_key for e in index.entries} == {first: key, second: key}


def _version(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def test_every_committed_card_and_index_still_loads():
    cards = [
        *REPO_ROOT.joinpath("docs").rglob(names.FAILURE_CARD),
        *REPO_ROOT.joinpath("apps/dashboard/src/fixtures").rglob(names.FAILURE_CARD),
    ]
    indexes = [*REPO_ROOT.joinpath("docs").rglob(names.RUN_INDEX)]
    assert len(cards) >= 6 and len(indexes) >= 3
    versions = set()
    for path in cards:
        card = FailureCard.model_validate_json(path.read_text(encoding="utf-8"))
        versions.add(_version(card.schema_version) >= (0, 5, 0))
        if _version(card.schema_version) < (0, 5, 0):
            assert (card.bundle_key, card.occurrences) == (None, [])
        else:
            assert card.bundle_key is not None
            assert card.occurrences[0].run_id == card.run_id
    assert versions == {True, False}  # both branches above ran
    # Every committed index predates 0.6.0 and has no bundle_key field, so
    # loading one is a real test of reading an older index.
    for path in indexes:
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert _version(raw["schema_version"]) < _version(RUN_INDEX_SCHEMA_VERSION)
        assert not any("bundle_key" in entry for entry in raw["entries"])
        index = RunIndex.model_validate(raw)
        assert index.entries and all(entry.bundle_key is None for entry in index.entries)
