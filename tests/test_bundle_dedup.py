"""#211: one failure card per root cause.

The bundle stage writes a card only for a key the runs directory has not seen.
A run whose key matches an existing card is appended to that card's occurrences
and gets a ``bundle_ref.json`` pointer in place of its own card, repair package
and regression artifact. The key itself is pinned in test_bundle_key.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import FAILURE_TASK_PATH, FIXTURES_DIR, REPO_ROOT
from trace_harness.attribution.schemas import AttributionResult
from trace_harness.cli import main
from trace_harness.failure_bundles.generator import (
    BundleKeyConflictError,
    FailureBundle,
    FailureBundleGenerator,
    record_bundle,
)
from trace_harness.failure_bundles.schemas import (
    FAILURE_CARD_SCHEMA_VERSION,
    BundleRef,
    FailureCard,
)
from trace_harness.regression.schemas import RegressionArtifact
from trace_harness.run_reader import RunReader
from trace_harness.runner.batch import BatchRunner
from trace_harness.runner.collector import collect_regressions
from trace_harness.runner.pipeline import run_task_pipeline
from trace_harness.runner.report import build_suite_report
from trace_harness.runner.result import RunResult
from trace_harness.runner.suite import AgentConfig, load_suite
from trace_harness.tasks.schemas import TaskSpec
from trace_harness.tracing import artifact_store as names
from trace_harness.tracing.artifact_store import ArtifactStore
from trace_harness.verifiers.base import VerifierResult

MISSING_INFO_FAILURE = FIXTURES_DIR / "tasks" / "refund_policy_missing_info_failure.json"


def _pipeline(task: Path, store: ArtifactStore, **config) -> str:
    agent = AgentConfig(label=config.pop("label", "fixture"), **config)
    return run_task_pipeline(task, agent, store).run_result.run_id


def _unbundled(task: Path, store: ArtifactStore) -> str:
    """Run, verify and attribute, stopping before the bundle stage."""
    run_id = run_task_pipeline(
        task, AgentConfig(label="fixture"), store, bundle_on_fail=False
    ).run_result.run_id
    assert main(["attribute", str(store.run_dir(run_id))]) == 0
    return run_id


def _card(store: ArtifactStore, run_id: str) -> FailureCard:
    return FailureCard.model_validate(store.read_json(run_id, names.FAILURE_CARD))


def _cards(store: ArtifactStore) -> list[str]:
    return sorted(p.parent.name for p in store.runs_dir.glob(f"*/{names.FAILURE_CARD}"))


def _bundle_for(store: ArtifactStore, run_id: str) -> FailureBundle:
    """Generate a run's bundle from its artifacts on disk, without recording it."""
    config = store.read_json(run_id, names.RUN_CONFIG)
    return FailureBundleGenerator().generate(
        task=TaskSpec.model_validate(store.read_json(run_id, names.TASK_SPEC)),
        run_result=RunResult.model_validate(store.read_json(run_id, names.RUN_RESULT)),
        trace=store.read_trace(run_id),
        verifier_result=VerifierResult.model_validate(
            store.read_json(run_id, names.VERIFIER_RESULT)
        ),
        attribution=AttributionResult.model_validate(
            store.read_json(run_id, names.ATTRIBUTION_RESULT)
        ),
        final_state=store.read_json(run_id, names.FINAL_STATE),
        initial_state=store.read_json(run_id, names.INITIAL_STATE),
        task_fixture_path=config["metadata"].get("task_fixture_path"),
        run_config=config,
    )


# --- the dedup ----------------------------------------------------------


def test_bundling_the_same_failing_task_twice_yields_one_card_with_two_occurrences(tmp_path):
    store = ArtifactStore(tmp_path / "runs")
    first = _unbundled(FAILURE_TASK_PATH, store)
    second = _unbundled(FAILURE_TASK_PATH, store)

    assert main(["bundle", str(store.run_dir(first))]) == 0
    assert main(["bundle", str(store.run_dir(second))]) == 0

    assert _cards(store) == [first]
    card = _card(store, first)
    assert card.schema_version == FAILURE_CARD_SCHEMA_VERSION
    assert card.run_id == first
    assert [o.run_id for o in card.occurrences] == [first, second]
    assert {o.task_id for o in card.occurrences} == {"refund_policy_failure"}

    # One repair package and one regression artifact, pinned to the first.
    for name in (names.REPAIR_PACKAGE, names.REGRESSION_ARTIFACT):
        assert sorted(p.parent.name for p in store.runs_dir.glob(f"*/{name}")) == [first]
    regression = RegressionArtifact.model_validate(
        store.read_json(first, names.REGRESSION_ARTIFACT)
    )
    assert regression.source_run_id == first

    ref = BundleRef.model_validate(store.read_json(second, names.BUNDLE_REF))
    assert (ref.run_id, ref.canonical_run_id, ref.bundle_key) == (second, first, card.bundle_key)
    assert {e.run_id: e.bundle_key for e in store.read_index().entries} == {
        first: card.bundle_key,
        second: card.bundle_key,
    }


def test_runs_failing_on_different_check_sets_yield_two_cards(tmp_path):
    store = ArtifactStore(tmp_path / "runs")
    refund = _pipeline(FAILURE_TASK_PATH, store)
    escalation = _pipeline(MISSING_INFO_FAILURE, store)

    assert _cards(store) == sorted([refund, escalation])
    assert not list(store.runs_dir.glob(f"*/{names.BUNDLE_REF}"))
    refund_card, escalation_card = _card(store, refund), _card(store, escalation)
    assert refund_card.bundle_key != escalation_card.bundle_key
    assert [o.run_id for o in refund_card.occurrences] == [refund]
    assert [o.run_id for o in escalation_card.occurrences] == [escalation]


def test_a_sweep_records_each_configuration_on_its_occurrence(tmp_path):
    """The batch path dedups too, and says which configuration hit the failure."""
    store = ArtifactStore(tmp_path / "runs")
    suite = load_suite(FIXTURES_DIR / "suites" / "multi_config.json").model_copy(
        update={
            "tasks": [str(FAILURE_TASK_PATH)],
            "agent_configs": [
                AgentConfig(label="seed-1", provider="fixture", seed=1),
                AgentConfig(label="seed-2", provider="fixture", seed=2),
            ],
        }
    )
    summary = BatchRunner(store).run(suite)
    first, second = (entry.run_id for entry in summary.entries)

    assert _cards(store) == [first]
    occurrences = _card(store, first).occurrences
    assert [(o.run_id, o.provider, o.seed) for o in occurrences] == [
        (first, "fixture", 1),
        (second, "fixture", 2),
    ]
    assert occurrences[0].model == "scripted:refund_policy_failure_script"

    # The report credits the reproduction to the first occurrence's regression.
    rows = build_suite_report(summary, store).rows
    assert [r.regression_test_name for r in rows] == ["regression_refund_policy_failure"] * 2


# --- idempotency --------------------------------------------------------


def test_bundling_a_run_again_never_adds_it_twice(tmp_path):
    store = ArtifactStore(tmp_path / "runs")
    first = _pipeline(FAILURE_TASK_PATH, store)
    second = _pipeline(FAILURE_TASK_PATH, store)
    card_bytes = store.artifact_path(first, names.FAILURE_CARD).read_bytes()
    ref_bytes = store.artifact_path(second, names.BUNDLE_REF).read_bytes()

    for run_id in (second, first, second, first):
        assert main(["bundle", str(store.run_dir(run_id))]) == 0

    assert _cards(store) == [first]
    assert [o.run_id for o in _card(store, first).occurrences] == [first, second]
    assert store.artifact_path(first, names.FAILURE_CARD).read_bytes() == card_bytes
    assert store.artifact_path(second, names.BUNDLE_REF).read_bytes() == ref_bytes
    assert not store.exists(second, names.FAILURE_CARD)


def test_a_reproduction_whose_key_changes_leaves_its_old_card(tmp_path):
    store = ArtifactStore(tmp_path / "runs")
    first = _pipeline(FAILURE_TASK_PATH, store)
    second = _pipeline(FAILURE_TASK_PATH, store)

    moved = _bundle_for(store, second)
    moved.failure_card.bundle_key = "v1:policy_violation:none:0000000000000000"
    recorded = record_bundle(store, moved)

    assert not recorded.reproduction
    assert _cards(store) == sorted([first, second])
    assert [o.run_id for o in _card(store, first).occurrences] == [first]
    assert not store.exists(second, names.BUNDLE_REF)
    entries = {e.run_id: e.bundle_key for e in store.read_index().entries}
    assert entries[second] == "v1:policy_violation:none:0000000000000000"


def test_a_card_other_runs_point_to_refuses_a_new_key(tmp_path):
    store = ArtifactStore(tmp_path / "runs")
    first = _pipeline(FAILURE_TASK_PATH, store)
    second = _pipeline(FAILURE_TASK_PATH, store)
    before = store.artifact_path(first, names.FAILURE_CARD).read_bytes()

    moved = _bundle_for(store, first)
    moved.failure_card.bundle_key = "v1:policy_violation:none:0000000000000000"
    with pytest.raises(BundleKeyConflictError):
        record_bundle(store, moved)

    assert store.artifact_path(first, names.FAILURE_CARD).read_bytes() == before
    assert store.exists(second, names.BUNDLE_REF)


def test_an_index_entry_without_its_card_never_resolves(tmp_path):
    """An interrupted bundle leaves the entry and no card, and the next run starts one."""
    store = ArtifactStore(tmp_path / "runs")
    first = _unbundled(FAILURE_TASK_PATH, store)
    key = _bundle_for(store, first).failure_card.bundle_key
    store.set_index_bundle_key(first, key)

    second = _pipeline(FAILURE_TASK_PATH, store)

    assert store.find_bundle_card(key) == second
    assert _cards(store) == [second]


def test_concurrent_bundles_of_one_key_share_one_card(tmp_path):
    """Two processes bundling the same failure at once still write one card.

    Each child waits after its key lookup until the other has looked too, or
    three seconds pass. Without the runs directory's bundle lock both lookups
    miss and both write a card. With it the second child is still waiting for
    the lock, so the first gives up waiting, writes, and the second finds its
    card.
    """
    store = ArtifactStore(tmp_path / "runs")
    first = _unbundled(FAILURE_TASK_PATH, store)
    second = _unbundled(FAILURE_TASK_PATH, store)
    sync = tmp_path / "sync"
    sync.mkdir()
    child = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from trace_harness.cli import main\n"
        "from trace_harness.tracing.artifact_store import ArtifactStore\n"
        "sync, name = Path(sys.argv[2]), sys.argv[3]\n"
        "lookup = ArtifactStore.find_bundle_card\n"
        "def rendezvous(self, key):\n"
        "    found = lookup(self, key)\n"
        "    (sync / f'looked-{name}').touch()\n"
        "    deadline = time.monotonic() + 3\n"
        "    while len(list(sync.glob('looked-*'))) < 2 and time.monotonic() < deadline:\n"
        "        time.sleep(0.01)\n"
        "    return found\n"
        "ArtifactStore.find_bundle_card = rendezvous\n"
        "(sync / f'ready-{name}').touch()\n"
        "while not (sync / 'go').exists():\n"
        "    time.sleep(0.005)\n"
        "sys.exit(main(['bundle', sys.argv[1]]))\n"
    )
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(REPO_ROOT / "src"), *sys.path])}
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", child, str(store.run_dir(run_id)), str(sync), name],
            env=env,
            stdout=subprocess.DEVNULL,
        )
        for name, run_id in (("a", first), ("b", second))
    ]
    deadline = time.monotonic() + 60
    while len(list(sync.glob("ready-*"))) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    (sync / "go").touch()
    assert [p.wait(timeout=120) for p in procs] == [0, 0]

    (holder,) = _cards(store)
    assert sorted(o.run_id for o in _card(store, holder).occurrences) == sorted([first, second])


# --- reading and the gate -----------------------------------------------


def test_the_reader_serves_a_reproduction_the_card_it_joined(tmp_path):
    store = ArtifactStore(tmp_path / "runs")
    first = _pipeline(FAILURE_TASK_PATH, store)
    second = _pipeline(FAILURE_TASK_PATH, store)
    reader = RunReader(store)

    joined = reader.get_bundle(second)
    assert joined is not None
    assert joined == reader.get_bundle(first)
    assert joined.failure_card.run_id == first
    assert joined.regression_artifact.source_run_id == first
    assert {s.run_id: s.bundle_key for s in reader.list_runs()} == {
        first: joined.failure_card.bundle_key,
        second: joined.failure_card.bundle_key,
    }


@pytest.mark.parametrize("canonical", ["../elsewhere", "a/b", "..", "", "C:run"])
def test_a_pointer_can_only_name_a_sibling_run(tmp_path, canonical):
    store = ArtifactStore(tmp_path / "runs")
    store.create_run_dir("run_x")
    store.write_json(
        "run_x",
        names.BUNDLE_REF,
        {"run_id": "run_x", "task_id": "t", "bundle_key": "k", "canonical_run_id": canonical},
    )
    with pytest.raises(ValueError):
        store.bundle_home("run_x")
    with pytest.raises(ValueError):
        BundleRef.model_validate(store.read_json("run_x", names.BUNDLE_REF))


def test_the_suite_gate_replays_one_artifact_per_key(tmp_path, monkeypatch):
    """Reproductions are covered by their first occurrence's artifact and are not artifacts."""
    monkeypatch.chdir(REPO_ROOT)
    manifest = tmp_path / "sweep.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "0.1.0",
                "suite_id": "sweep",
                "tasks": [str(FAILURE_TASK_PATH.relative_to(REPO_ROOT))],
                "agent_configs": [
                    {"label": "a", "provider": "fixture", "seed": 1},
                    {"label": "b", "provider": "fixture", "seed": 2},
                ],
            }
        )
    )
    empty = tmp_path / "retained"
    empty.mkdir()

    summary = collect_regressions(empty, ArtifactStore(tmp_path / "out"), suite_path=manifest)

    assert summary.errors == [] and summary.malformed == []
    assert summary.artifacts_found == summary.blocking == summary.reproduced == 1
    assert summary.exit_code == 0


# --- cards written before 0.5.0 -----------------------------------------


def _as_written_before_keys(store: ArtifactStore, run_id: str) -> None:
    data = store.read_json(run_id, names.FAILURE_CARD)
    data["schema_version"] = "0.4.0"
    del data["bundle_key"], data["occurrences"]
    store.write_json(run_id, names.FAILURE_CARD, data)


def test_a_card_written_before_keys_is_only_matched_once_rebundled(tmp_path):
    store = ArtifactStore(tmp_path / "runs")
    old = _pipeline(FAILURE_TASK_PATH, store)
    _as_written_before_keys(store, old)
    store.rebuild_index()

    new = _pipeline(FAILURE_TASK_PATH, store)
    assert _cards(store) == sorted([old, new])  # the old card keeps its own identity
    assert [o.run_id for o in _card(store, new).occurrences] == [new]

    assert main(["bundle", str(store.run_dir(old))]) == 0
    assert _cards(store) == [new]
    assert [o.run_id for o in _card(store, new).occurrences] == [new, old]
    assert not store.exists(old, names.REGRESSION_ARTIFACT)
    assert not store.exists(old, names.REPAIR_PACKAGE)
