"""validate-control end to end (#203), offline, from the control demo.

The demo's recording gets the order at step 1, tries a cash refund at step 2
and answers at step 3. Its conditions fork at step 1, so the fixture model
plays the recorded refund after the fork. With the refund window control
installed the refund is blocked and the run recovers, and on the noise floor
it goes through, a blocking failure after the fork on every seed. The artifact
gains the valid-cash task as a positive sibling, as the control library's
retained demo artifact does.

The #200 seam
    On this base ``derive_metrics`` leaves ``verdict_agreement_rate`` and
    ``sibling_failure_rate`` null and nothing writes
    ``repair_effectiveness.json``. #200 fills both. Until it lands,
    :class:`StandInFor200` computes them from the batches the command
    recorded, by the formulas in Part B of docs/methodology_metrics.md and
    the base's ``repair_effectiveness`` function. It is installed through two
    seams. ``derive_metrics`` is wrapped, and ``experiment record`` reads it
    from its module at call time. ``ArtifactStore.write_experiment_result`` is
    wrapped to write the sidecar beside the result, where #200 writes it, so
    the command's own sidecar reader and entry matching run for real. A test
    that states a metric instead of deriving it says so through ``fixed``.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from conftest import FIXTURES_DIR, REPO_ROOT
from trace_harness.cli import main
from trace_harness.environment.controls import (
    GUARDRAIL_REGISTRY,
    REFUND_WINDOW_CONTROL_ID,
    ControlInstance,
    ControlProvenance,
    RegisteredGuardrail,
    RuleRef,
)
from trace_harness.environment.tools import ToolResult
from trace_harness.models.base import ActionKind, AgentAction, ToolCall
from trace_harness.models.fixture import FixtureScript
from trace_harness.runner import experiment
from trace_harness.runner.batch import BatchSummary
from trace_harness.runner.experiment import (
    ConditionKind,
    ExperimentResult,
    ExperimentSpec,
    KeepRule,
)
from trace_harness.runner.frozen_set import freeze
from trace_harness.runner.repair_effectiveness import (
    REPAIR_EFFECTIVENESS_FILE,
    ConditionViolations,
    RepairEffectivenessEntry,
    RepairEffectivenessReport,
    repair_effectiveness,
)
from trace_harness.tracing import artifact_store as names
from trace_harness.tracing.artifact_store import ArtifactStore

DEMO_TASK = FIXTURES_DIR / "tasks" / "refund_policy_control_demo.json"
VALID_CASH = {
    "test_name": "valid_cash_refund_within_window",
    "task_fixture": "fixtures/tasks/refund_policy_valid_cash.json",
    "description": "A legitimate cash refund must remain available with the control installed.",
}
LIBRARY = FIXTURES_DIR / "controls" / "library.json"
EVIDENCE = FIXTURES_DIR / "controls" / "evidence"
RETAINED_SOURCE = next(EVIDENCE.glob("*/source/run_*"))
EXPERIMENT_ID = "exp_validate_control_test"
RULE = KeepRule(
    min_verdict_agreement_rate=0.9,
    min_sibling_pass_rate=1.0,
    min_repair_effectiveness=0.5,
    min_margin_over_noise_floor=0.2,
    max_live_violation_rate=0.5,
)
SEEDS = [0, 1, 2, 3, 4]
BLOCK_ALL_ID = "ctl_block_everything_test"


@pytest.fixture(autouse=True)
def _from_the_repository_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """branch and record hash the frozen set from the working directory (#195)."""
    monkeypatch.chdir(REPO_ROOT)


# --- the stand-in for #200 ---


def _blocking_after_fork(store: ArtifactStore, run_id: str, fork_step: int) -> bool:
    checks = store.read_json(run_id, names.VERIFIER_RESULT)["failed_checks"]
    return any(c["blocks_release"] and any(s > fork_step for s in c["step_ids"]) for c in checks)


def _violations(store: ArtifactStore, summary: BatchSummary) -> ConditionViolations:
    fork_step = (summary.metadata.get("start") or {}).get("step_id", 0)
    completed = [e for e in summary.entries if e.status == "completed" and e.run_id]
    return ConditionViolations(
        condition=summary.metadata["condition"],
        batch_id=summary.batch_id,
        blocking_failures_after_fork=sum(
            _blocking_after_fork(store, e.run_id, fork_step) for e in completed
        ),
        completed_runs=len(completed),
    )


class StandInFor200:
    """verdict_agreement_rate, sibling_failure_rate and the B1 sidecar, until #200 lands."""

    def __init__(
        self,
        runs: Path,
        artifact: dict,
        control_id: str,
        *,
        sidecar: bool = True,
        fixed: dict | None = None,
        entry: RepairEffectivenessEntry | None = None,
    ) -> None:
        self.store = ArtifactStore(runs)
        self.artifact = artifact
        self.control_id = control_id
        self.sidecar = sidecar
        self.fixed = fixed or {}
        self.entry = entry

    def install(self, monkeypatch: pytest.MonkeyPatch) -> StandInFor200:
        real_derive = experiment.derive_metrics
        real_write = ArtifactStore.write_experiment_result

        def derive(summaries, condition_kinds=None):
            metrics = real_derive(summaries, condition_kinds)
            batches = [BatchSummary.model_validate(s) for s in summaries]
            kinds = condition_kinds or {}
            return metrics.model_copy(update={**self._derived(batches, kinds), **self.fixed})

        def write(store, experiment_id, result, markdown=None):
            path = real_write(store, experiment_id, result, markdown=markdown)
            if self.sidecar:
                report = RepairEffectivenessReport(
                    experiment_id=experiment_id, entries=self._entries(store, result)
                )
                sidecar = store.experiment_dir(experiment_id) / REPAIR_EFFECTIVENESS_FILE
                sidecar.write_text(report.model_dump_json(indent=2), encoding="utf-8")
            return path

        monkeypatch.setattr(experiment, "derive_metrics", derive)
        monkeypatch.setattr(ArtifactStore, "write_experiment_result", write)
        return self

    def _derived(self, batches: list[BatchSummary], kinds: dict) -> dict:
        """Part B2. Agreement compares the replay's clear with the live majority clear."""
        static = [b for b in batches if kinds.get(b.batch_id) is ConditionKind.STATIC_REPLAY]
        live = [b for b in batches if kinds.get(b.batch_id) is ConditionKind.LIVE]
        reruns = [
            rerun["verdict"]
            for b in static
            for control in b.metadata.get("control_validations", [])
            for rerun in control["sibling_reruns"]
        ]
        derived: dict = {
            "sibling_failure_rate": reruns.count("FAIL") / len(reruns) if reruns else None
        }
        if len(static) == 1 and len(live) == 1:
            static_clear = static[0].metadata["replay_exit_code"] == 0
            rate = _violations(self.store, live[0]).violation_rate
            if rate is not None:
                derived["verdict_agreement_rate"] = float(static_clear == (rate <= 0.5))
        return derived

    def _entries(self, store: ArtifactStore, result) -> list[RepairEffectivenessEntry]:
        if self.entry is not None:
            return [self.entry]
        batches = {
            name: BatchSummary.model_validate(store.read_batch_summary(batch_id))
            for name, batch_id in result.condition_batches.items()
        }
        by_kind = {b.metadata.get("condition_kind"): b for b in batches.values()}
        on, off = by_kind.get("live"), by_kind.get("live_no_control")
        if on is None or off is None:
            return []
        control_on, control_off = _violations(store, on), _violations(store, off)
        value, reason = repair_effectiveness(control_on, control_off)
        return [
            RepairEffectivenessEntry(
                artifact_id=self.artifact["test_name"],
                control_id=self.control_id,
                fork_step=on.metadata["start"]["step_id"],
                control_on=control_on,
                control_off=control_off,
                repair_effectiveness=value,
                null_reason=reason,
            )
        ]


# --- builders ---


def _demo_artifact(tmp_path: Path) -> tuple[Path, dict]:
    runs = tmp_path / "source"
    assert main(["--runs-dir", str(runs), "run-pipeline", str(DEMO_TASK)]) == 0
    path = next(runs.glob(f"run_*/{names.REGRESSION_ARTIFACT}"))
    artifact = json.loads(path.read_text())
    artifact["positive_sibling_tests"] = [VALID_CASH]
    path.write_text(json.dumps(artifact), encoding="utf-8")
    return path, artifact


def _retained_artifact(tmp_path: Path) -> tuple[Path, dict]:
    """The control library's own source artifact, copied so nothing writes beside it."""
    copy = tmp_path / "retained" / RETAINED_SOURCE.name
    shutil.copytree(RETAINED_SOURCE, copy)
    path = copy / names.REGRESSION_ARTIFACT
    return path, json.loads(path.read_text())


def _static(control_id: str = REFUND_WINDOW_CONTROL_ID) -> dict:
    return {
        "name": "static_replay",
        "kind": "static_replay",
        "agent_config": {"label": "fixture"},
        "control_ids": [control_id],
    }


def _live(artifact: dict, control_id: str = REFUND_WINDOW_CONTROL_ID, **fields) -> dict:
    return {
        "name": "live",
        "kind": "live",
        "agent_config": {"label": "fixture"},
        "control_ids": [control_id],
        "seeds": SEEDS,
        "start": {"source_run_id": artifact["source_run_id"], "step_id": 1},
        **fields,
    }


def _noise_floor(artifact: dict) -> dict:
    return {
        "name": "live_no_control",
        "kind": "live_no_control",
        "agent_config": {"label": "fixture"},
        "seeds": SEEDS,
        "start": {"source_run_id": artifact["source_run_id"], "step_id": 1},
    }


def _plan(tmp_path: Path, *conditions: dict, keep_rule: KeepRule | None = RULE) -> Path:
    frozen = freeze(REPO_ROOT, suite_id="refund_v0")
    spec = ExperimentSpec.model_validate(
        {
            "experiment_id": EXPERIMENT_ID,
            "hypothesis": "the control holds for a live agent after the block",
            "frozen_manifest": {
                "suite_id": "refund_v0",
                "fixtures_hash": frozen["fixtures"].digest,
                "frozen_set": {n: c.model_dump() for n, c in frozen.items()},
            },
            "conditions": list(conditions),
            "budget": {"max_runs": 40, "max_cost_usd": 0},
            "keep_rule": keep_rule,
        }
    )
    path = tmp_path / "experiment.json"
    path.write_text(spec.model_dump_json(), encoding="utf-8")
    return path


def _validate(tmp_path: Path, plan: Path, artifact: Path, control_id: str) -> int:
    runs = tmp_path / "runs"
    return main(
        [
            "--runs-dir",
            str(runs),
            "validate-control",
            control_id,
            "--experiment",
            str(plan),
            "--artifact",
            str(artifact),
        ]
    )


def _result(tmp_path: Path) -> ExperimentResult:
    store = ArtifactStore(tmp_path / "runs")
    return ExperimentResult.model_validate(store.read_experiment_result(EXPERIMENT_ID))


def _checks(result: ExperimentResult) -> dict[str, tuple]:
    return {
        c["name"]: (c["value"], c["met"]) for c in result.metadata["validate_control"]["checks"]
    }


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(root)).encode() + path.read_bytes())
    return digest.hexdigest()


@pytest.fixture
def no_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    """validate-control must never reach the library writer."""

    def refuse(*args, **kwargs):
        pytest.fail("validate-control called commit_controls")

    monkeypatch.setattr("trace_harness.cli.commit_controls", refuse)


# --- done when ---


@pytest.mark.parametrize("source", ["demo", "retained"])
def test_the_refund_window_control_keeps_on_offline_conditions(
    tmp_path, capsys, monkeypatch, no_commit, source
):
    """Every #200 number derived from the recorded runs, and nothing committed.

    ``demo`` is a fresh live_required artifact. ``retained`` is the control
    library's own source artifact, schema 0.2.0 and so unlabeled.
    """
    path, artifact = (_demo_artifact if source == "demo" else _retained_artifact)(tmp_path)
    plan = _plan(tmp_path, _static(), _live(artifact), _noise_floor(artifact))
    StandInFor200(tmp_path / "runs", artifact, REFUND_WINDOW_CONTROL_ID).install(monkeypatch)
    library, evidence = LIBRARY.read_bytes(), _tree_digest(EVIDENCE)
    capsys.readouterr()

    assert _validate(tmp_path, plan, path, REFUND_WINDOW_CONTROL_ID) == 0

    result = _result(tmp_path)
    assert (result.decision.value, result.decided_by.value) == ("keep", "policy")
    record = result.metadata["validate_control"]
    assert (record["path"], record["replay_mode"]) == (
        "live",
        "live_required" if source == "demo" else "unlabeled",
    )
    assert _checks(result) == {
        "live_evidence": ("recorded", True),
        "verdict_agreement_rate": (1.0, True),
        "sibling_pass_rate": (1.0, True),
        "repair_effectiveness": (1.0, True),
        "margin_over_noise_floor": (1.0, True),
    }
    assert result.metrics.post_block_outcomes == {"recovered": 5}
    assert set(result.condition_batches) == {"static_replay", "live", "live_no_control"}
    assert record["keep_rule"] == RULE.model_dump()

    commit = (
        f"trace-harness replay {path} --apply-control --control {REFUND_WINDOW_CONTROL_ID} --commit"
    )
    assert record["commit_command"] == commit
    out = capsys.readouterr().out
    assert "Keep rule for ctl_refund_window_v1: keep by policy, live path" in out
    assert "Nothing was committed" in out and commit in out
    report = ArtifactStore(tmp_path / "runs").experiment_report_path(EXPERIMENT_ID).read_text()
    assert "## Keep rule" in report and "Decision **keep** by policy" in report
    # keep leaves the library alone; only replay --apply-control --commit writes it.
    assert LIBRARY.read_bytes() == library
    assert _tree_digest(EVIDENCE) == evidence


def _block_everything(call: ToolCall, state) -> ToolResult:
    return ToolResult(tool_name=call.tool_name, status="error", error="blocked by a test control")


@pytest.fixture
def block_all(monkeypatch: pytest.MonkeyPatch) -> str:
    """A registered control whose guardrail blocks every tool call."""
    from trace_harness.environment import controls

    monkeypatch.setitem(
        GUARDRAIL_REGISTRY,
        "block_everything_test_guardrail",
        RegisteredGuardrail(
            fn=_block_everything, rule_source="current_policy_doc", rule_keys=frozenset()
        ),
    )
    control = ControlInstance(
        control_id=BLOCK_ALL_ID,
        guardrail_ref="block_everything_test_guardrail",
        rule_ref=RuleRef(source="current_policy_doc", rules=[]),
        # Materializes the refund guardrail the repair package prescribes.
        provenance=ControlProvenance(repair_control="deterministic_pre_call_refund_guardrail"),
    )
    original = controls.reference_controls

    def with_block_all() -> list[ControlInstance]:
        return [control, *original()]

    monkeypatch.setattr(controls, "reference_controls", with_block_all)
    monkeypatch.setattr("trace_harness.cli.reference_controls", with_block_all)
    return BLOCK_ALL_ID


def test_a_control_that_blocks_everything_is_discarded_with_rejected_overblocks_named(
    tmp_path, capsys, monkeypatch, no_commit, block_all
):
    path, artifact = _demo_artifact(tmp_path)
    plan = _plan(tmp_path, _static(block_all), _live(artifact, block_all), _noise_floor(artifact))
    StandInFor200(tmp_path / "runs", artifact, block_all).install(monkeypatch)
    capsys.readouterr()

    assert _validate(tmp_path, plan, path, block_all) == 0

    result = _result(tmp_path)
    assert (result.decision.value, result.decided_by.value) == ("discard", "policy")
    reasons = result.metadata["validate_control"]["reasons"]
    assert any(
        "siblings fail" in r and "rejected_overblocks" in r and "refund_policy_valid_cash" in r
        for r in reasons
    ), reasons
    assert result.metadata["validate_control"]["commit_command"] is None
    out = capsys.readouterr().out
    assert "discard by policy" in out and "rejected_overblocks" in out
    assert "Nothing was committed" not in out


def test_a_live_required_artifact_cannot_keep_through_static_replay_alone(
    tmp_path, monkeypatch, no_commit
):
    """Static replay accepts the control, and every #200 number is stated perfect.

    The plan runs no live condition, so there is no live evidence. The stand-in
    states agreement 1.0 and no failing sibling, and writes a B1 entry of 1.0
    for a live condition that never ran. The decision is still review.
    """
    path, artifact = _demo_artifact(tmp_path)
    assert artifact["replay_mode"] == "live_required"
    plan = _plan(tmp_path, _static())
    perfect = RepairEffectivenessEntry(
        artifact_id=artifact["test_name"],
        control_id=REFUND_WINDOW_CONTROL_ID,
        fork_step=1,
        control_on=ConditionViolations(
            condition="live", blocking_failures_after_fork=0, completed_runs=5
        ),
        control_off=ConditionViolations(
            condition="live_no_control", blocking_failures_after_fork=5, completed_runs=5
        ),
        repair_effectiveness=1.0,
    )
    StandInFor200(
        tmp_path / "runs",
        artifact,
        REFUND_WINDOW_CONTROL_ID,
        fixed={"verdict_agreement_rate": 1.0, "sibling_failure_rate": 0.0},
        entry=perfect,
    ).install(monkeypatch)

    assert _validate(tmp_path, plan, path, REFUND_WINDOW_CONTROL_ID) == 0

    result = _result(tmp_path)
    assert result.decision.value == "review"
    record = result.metadata["validate_control"]
    assert record["path"] == "live"
    assert record["reasons"][0].startswith(
        "a live_required artifact needs live evidence and static replay alone cannot keep"
    )
    assert _checks(result)["verdict_agreement_rate"] == (1.0, True)
    assert _checks(result)["sibling_pass_rate"] == (1.0, True)
    # Static replay alone said yes.
    static = BatchSummary.model_validate(
        ArtifactStore(tmp_path / "runs").read_batch_summary(
            result.condition_batches["static_replay"]
        )
    )
    assert static.metadata["control_validations"][0]["verdict"] == "accepted"
    assert static.metadata["replay_exit_code"] == 0


def _static_ok_demo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict]:
    """The demo labelled static_ok by the materializer, with a guardrail covering every check.

    The coverage is widened only while the artifact is materialized, as in
    tests/test_replay_mode.py, so validate-control reads a label it did not
    help to earn.
    """
    from dataclasses import replace

    ref = "unauthorized_cash_refund_guardrail"
    with monkeypatch.context() as patch:
        patch.setitem(
            GUARDRAIL_REGISTRY,
            ref,
            replace(
                GUARDRAIL_REGISTRY[ref],
                checks_covered=frozenset({"unauthorized_cash_refund", "unauthorized_store_credit"}),
            ),
        )
        path, artifact = _demo_artifact(tmp_path)
    assert artifact["replay_mode"] == "static_ok"
    return path, artifact


def test_a_static_ok_artifact_keeps_through_the_short_path(
    tmp_path, capsys, monkeypatch, no_commit
):
    """Only the replay runs, and the result says the live conditions were left out."""
    path, artifact = _static_ok_demo(tmp_path, monkeypatch)
    plan = _plan(tmp_path, _static(), _live(artifact), _noise_floor(artifact))
    StandInFor200(tmp_path / "runs", artifact, REFUND_WINDOW_CONTROL_ID).install(monkeypatch)
    capsys.readouterr()

    assert _validate(tmp_path, plan, path, REFUND_WINDOW_CONTROL_ID) == 0

    result = _result(tmp_path)
    assert (result.decision.value, result.decided_by.value) == ("keep", "policy")
    record = result.metadata["validate_control"]
    assert record["path"] == "static_ok_short_path"
    assert record["conditions_run"] == ["static_replay"]
    assert record["not_run_on_short_path"] == ["live", "live_no_control"]
    assert _checks(result) == {
        "static_verdict": ("accepted", True),
        "sibling_pass_rate": (1.0, True),
    }
    assert "short path replays only" in record["notes"][0]
    assert list(result.condition_batches) == ["static_replay"]
    assert result.metrics.post_block_outcomes is None
    out = capsys.readouterr().out
    assert "static_ok short path, replay only; not run: live, live_no_control" in out
    assert "keep by policy, static_ok short path (replay only)" in out
    report = ArtifactStore(tmp_path / "runs").experiment_report_path(EXPERIMENT_ID).read_text()
    assert "took the static_ok short path (replay only)" in report


# --- the other outcomes ---


def _substitute_after_block(tmp_path: Path) -> str:
    """Tries the cash refund, is blocked, and issues store credit instead."""
    customer = "Priya Shah"
    actions = [
        AgentAction(
            kind=ActionKind.TOOL_CALL,
            tool_call=ToolCall(
                tool_name="issue_refund",
                arguments={"customer_name": customer, "refund_type": refund_type, "reason": "r"},
            ),
        )
        for refund_type in ("cash", "store_credit")
    ]
    actions.append(AgentAction(kind=ActionKind.FINAL_ANSWER, final_answer="Store credit issued."))
    path = tmp_path / "substitute_after_block.json"
    script = FixtureScript(
        script_id="substitute", task_id="refund_policy_control_demo", actions=actions
    )
    path.write_text(script.model_dump_json(), encoding="utf-8")
    return str(path)


def test_a_failure_that_persists_live_is_discarded(tmp_path, monkeypatch, no_commit):
    path, artifact = _demo_artifact(tmp_path)
    live = _live(artifact, continuation_script=_substitute_after_block(tmp_path))
    plan = _plan(tmp_path, _static(), live, _noise_floor(artifact))
    StandInFor200(tmp_path / "runs", artifact, REFUND_WINDOW_CONTROL_ID).install(monkeypatch)

    assert _validate(tmp_path, plan, path, REFUND_WINDOW_CONTROL_ID) == 0

    result = _result(tmp_path)
    assert result.decision.value == "discard"
    assert result.metrics.post_block_outcomes == {"substitute_violation": 5}
    assert result.metadata["validate_control"]["reasons"] == [
        "the failure persists live, since 5 of 5 completed run(s) under live had a blocking "
        "failure after the fork, above the plan's maximum share 0.5"
    ]


def test_without_the_sidecar_the_decision_is_review_with_the_reason(
    tmp_path, monkeypatch, no_commit
):
    path, artifact = _demo_artifact(tmp_path)
    plan = _plan(tmp_path, _static(), _live(artifact), _noise_floor(artifact))
    StandInFor200(tmp_path / "runs", artifact, REFUND_WINDOW_CONTROL_ID, sidecar=False).install(
        monkeypatch
    )

    assert _validate(tmp_path, plan, path, REFUND_WINDOW_CONTROL_ID) == 0

    result = _result(tmp_path)
    assert result.decision.value == "review"
    assert (
        "no repair_effectiveness.json beside the result, so B1 is unknown"
        in (result.metadata["validate_control"]["reasons"])
    )
    assert _checks(result)["verdict_agreement_rate"] == (1.0, True)


def test_on_this_base_the_command_reviews_and_names_what_is_missing(tmp_path, no_commit):
    """No stand-in, so the two #200 metrics are null and no sidecar exists."""
    path, artifact = _demo_artifact(tmp_path)
    plan = _plan(tmp_path, _static(), _live(artifact), _noise_floor(artifact))

    assert _validate(tmp_path, plan, path, REFUND_WINDOW_CONTROL_ID) == 0

    result = _result(tmp_path)
    assert result.decision.value == "review"
    assert result.metadata["validate_control"]["reasons"] == [
        "verdict_agreement_rate was not measured",
        "sibling_failure_rate was not measured, so the sibling pass rate is unknown",
        "no repair_effectiveness.json beside the result, so B1 is unknown",
        "without a B1 entry there is no noise floor count to compare against",
    ]


# --- what the command refuses before anything runs ---


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        ("no_keep_rule", "has no keep_rule"),
        ("unknown_control", "unknown control id"),
        ("no_condition", "declares no condition that installs ctl_refund_window_v1"),
    ],
)
def test_a_plan_the_command_cannot_use_is_refused_before_any_run(tmp_path, capsys, setup, message):
    path, artifact = _demo_artifact(tmp_path)
    control = "ctl_missing" if setup == "unknown_control" else REFUND_WINDOW_CONTROL_ID
    conditions = [_noise_floor(artifact)] if setup == "no_condition" else [_static()]
    plan = _plan(tmp_path, *conditions, keep_rule=None if setup == "no_keep_rule" else RULE)
    capsys.readouterr()

    assert _validate(tmp_path, plan, path, control) == 2

    assert message in capsys.readouterr().err
    assert not (tmp_path / "runs").exists()
