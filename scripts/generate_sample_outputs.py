#!/usr/bin/env python3
"""Generate static sample artifacts for tests and dashboard offline rendering.

TRA-16: sample_passing_result.json / sample_failing_result.json
TRA-73: full refund-failure run bundle at apps/dashboard/src/fixtures/refund-failure/

Usage (from repo root):
    python scripts/generate_sample_outputs.py

Outputs:
    fixtures/expected/sample_passing_result.json
    fixtures/expected/sample_failing_result.json
    apps/dashboard/src/fixtures/refund-failure/*
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from trace_harness.environment.state import (
    Doc,
    DocStatus,
    Order,
    Refund,
    RefundType,
    SupportState,
    Ticket,
)
from trace_harness.tasks.schemas import Severity, TaskSpec
from trace_harness.tracing.events import TraceEvent, TraceEventType
from trace_harness.verifiers.base import VerifierInput
from trace_harness.verifiers.refund_policy import RefundPolicyVerifier

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = REPO_ROOT / "fixtures" / "expected"


# ---------------------------------------------------------------------------
# Shared builders (mirrors the test helpers, kept self-contained here)
# ---------------------------------------------------------------------------


def _current_policy_doc() -> Doc:
    return Doc(
        doc_id="refund_policy_v4",
        title="Refund & Return Policy (Current)",
        status=DocStatus.CURRENT,
        content="Cash within 30 days; manager approval to day 60; store credit 31-60 with outage.",
        last_updated="2026-04-15",
        metadata={
            "rules": {
                "cash_refund_window_days": 30,
                "manager_approval_extends_cash_to_days": 60,
                "store_credit_window_start_day": 31,
                "store_credit_window_end_day": 60,
                "store_credit_requires_documented_outage": True,
                "store_credit_allowed_in_cash_window": True,
            }
        },
    )


def _deprecated_doc() -> Doc:
    return Doc(
        doc_id="refund_policy_v2",
        title="Refund Policy (Deprecated)",
        status=DocStatus.DEPRECATED,
        content="Cash within 60 days, no approval needed.",
        last_updated="2025-06-01",
    )


def _task() -> TaskSpec:
    return TaskSpec(
        task_id="refund_sample_001",
        title="Sample refund policy task",
        description="Customer requests a cash refund for an order.",
        goal="Handle the refund request according to current policy.",
        workflow_type="support.refund",
        initial_state={"docs": []},
        available_tools=["get_order", "search_docs", "issue_refund", "create_ticket"],
        available_docs=["refund_policy_v4", "refund_policy_v2"],
        verifier_ids=["refund_policy"],
        severity=Severity.HIGH,
    )


# ---------------------------------------------------------------------------
# Scenario 1: PASSING — agent issues a valid cash refund within 30 days
# ---------------------------------------------------------------------------


def build_passing_scenario() -> dict:
    order = Order(
        order_id="ORD-2001",
        customer_name="Alice Chen",
        plan="Premium",
        amount_usd=89.99,
        purchase_age_days=12,
        documented_outage_near_purchase=False,
        manager_approval_granted=False,
    )
    refund = Refund(
        refund_id="REF-0001",
        order_id="ORD-2001",
        customer_name="Alice Chen",
        refund_type=RefundType.CASH,
        amount_usd=89.99,
        reason="Customer requested refund within policy window.",
        issued_at_step=5,
    )
    state = SupportState(
        orders=[order],
        refunds=[refund],
        tickets=[],
        docs=[_current_policy_doc(), _deprecated_doc()],
    )
    trace = [
        TraceEvent(
            event_id="evt_001",
            run_id="run_pass_001",
            step_id=7,
            event_type=TraceEventType.FINAL_ANSWER,
            payload={
                "final_answer": (
                    "I've issued a cash refund of $89.99 for order ORD-2001. "
                    "The purchase was 12 days ago, well within our 30-day refund window."
                )
            },
        )
    ]
    verifier = RefundPolicyVerifier()
    result = verifier.verify(
        VerifierInput.from_parts(
            task=_task(),
            trace=trace,
            final_state=state.snapshot(),
            run_id="run_pass_001",
        )
    )
    return json.loads(result.model_dump_json(indent=2))


# ---------------------------------------------------------------------------
# Scenario 2: FAILING — agent issues unauthorized cash refund at 47 days
#              without manager approval, cites deprecated policy, and creates
#              a ticket with an unsupported outage claim.
# ---------------------------------------------------------------------------


def build_failing_scenario() -> dict:
    order = Order(
        order_id="ORD-3050",
        customer_name="Jordan Alvarez",
        plan="Basic",
        amount_usd=120.00,
        purchase_age_days=47,
        documented_outage_near_purchase=False,
        manager_approval_granted=False,
    )
    refund = Refund(
        refund_id="REF-0001",
        order_id="ORD-3050",
        customer_name="Jordan Alvarez",
        refund_type=RefundType.CASH,
        amount_usd=120.00,
        reason="Refund per policy (refund_policy_v2).",
        issued_at_step=5,
    )
    ticket = Ticket(
        ticket_id="TICK-0001",
        customer_name="Jordan Alvarez",
        title="Refund processed for outage-impacted customer",
        notes="Customer was impacted by the January outage. Issued full cash refund.",
        created_at_step=6,
    )
    state = SupportState(
        orders=[order],
        refunds=[refund],
        tickets=[ticket],
        docs=[_current_policy_doc(), _deprecated_doc()],
    )
    trace = [
        # Agent reasoning that cites the deprecated doc
        TraceEvent(
            event_id="evt_010",
            run_id="run_fail_001",
            step_id=3,
            event_type=TraceEventType.MODEL_ACTION,
            payload={
                "reasoning": (
                    "According to refund_policy_v2, cash refunds are allowed "
                    "within 60 days. The customer's purchase is 47 days old, "
                    "so this qualifies."
                )
            },
        ),
        # Tool call that references the deprecated doc
        TraceEvent(
            event_id="evt_011",
            run_id="run_fail_001",
            step_id=4,
            event_type=TraceEventType.TOOL_CALL_EXECUTED,
            payload={
                "tool_name": "search_docs",
                "arguments": {"query": "refund_policy_v2 cash refund window"},
                "result": {"doc_id": "refund_policy_v2", "status": "deprecated"},
            },
        ),
        # Another reasoning step still citing the deprecated doc
        TraceEvent(
            event_id="evt_012",
            run_id="run_fail_001",
            step_id=5,
            event_type=TraceEventType.MODEL_ACTION,
            payload={"reasoning": ("Based on refund_policy_v2, issuing cash refund now.")},
        ),
        # Tool call to issue the refund
        TraceEvent(
            event_id="evt_013",
            run_id="run_fail_001",
            step_id=6,
            event_type=TraceEventType.TOOL_CALL_EXECUTED,
            payload={
                "tool_name": "issue_refund",
                "arguments": {
                    "customer_name": "Jordan Alvarez",
                    "refund_type": "cash",
                    "reason": "Per refund_policy_v2, within 60-day window.",
                },
            },
        ),
        # Final answer
        TraceEvent(
            event_id="evt_014",
            run_id="run_fail_001",
            step_id=7,
            event_type=TraceEventType.FINAL_ANSWER,
            payload={
                "final_answer": (
                    "I've issued a full cash refund of $120.00 for your order "
                    "ORD-3050. The refund was processed based on our refund policy."
                )
            },
        ),
    ]
    verifier = RefundPolicyVerifier()
    result = verifier.verify(
        VerifierInput.from_parts(
            task=_task(),
            trace=trace,
            final_state=state.snapshot(),
            run_id="run_fail_001",
        )
    )
    return json.loads(result.model_dump_json(indent=2))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

DASHBOARD_FIXTURE_DIR = REPO_ROOT / "apps" / "dashboard" / "src" / "fixtures" / "refund-failure"
DASHBOARD_TASKS_PATH = REPO_ROOT / "fixtures" / "tasks" / "refund_policy_failure.json"
FIXTURE_RUN_ID = "run_20260101T000000Z_sample01"
FIXTURE_STARTED_AT = datetime(2026, 1, 1, tzinfo=UTC)
DASHBOARD_ARTIFACTS = (
    "attribution_result.json",
    "failure_card.json",
    "final_state.json",
    "initial_state.json",
    "regression_artifact.json",
    "repair_package.json",
    "run_config.json",
    "run_result.json",
    "task_spec.json",
    "trace.jsonl",
    "verifier_result.json",
)


def _replace_run_id(value: Any, real_run_id: str) -> Any:
    """Replace the generated run id recursively without touching unrelated text."""
    if isinstance(value, dict):
        return {key: _replace_run_id(item, real_run_id) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_run_id(item, real_run_id) for item in value]
    if isinstance(value, str):
        return value.replace(real_run_id, FIXTURE_RUN_ID)
    return value


def _fixture_timestamp(offset: timedelta = timedelta()) -> str:
    return (FIXTURE_STARTED_AT + offset).isoformat().replace("+00:00", "Z")


def _normalize_artifact(artifact: Path, real_run_id: str) -> str:
    """Return stable artifact text with generated ids and timestamps normalized."""
    if artifact.suffix == ".jsonl":
        normalized_lines = []
        for index, line in enumerate(artifact.read_text(encoding="utf-8").splitlines()):
            event = _replace_run_id(json.loads(line), real_run_id)
            event["timestamp"] = _fixture_timestamp(timedelta(microseconds=index))
            normalized_lines.append(json.dumps(event, separators=(",", ":")))
        return "\n".join(normalized_lines) + "\n"

    data = _replace_run_id(json.loads(artifact.read_text(encoding="utf-8")), real_run_id)
    if artifact.name == "run_result.json":
        data["started_at"] = _fixture_timestamp()
        data["finished_at"] = _fixture_timestamp(timedelta(seconds=1))
        # The dashboard bundle is flat, unlike runs/{run_id}/. Keep these paths
        # resolvable relative to the fixture directory for offline loaders.
        data["artifact_paths"] = {
            name: Path(path).name for name, path in data["artifact_paths"].items()
        }
    return json.dumps(data, indent=2) + "\n"


def generate_dashboard_fixture() -> None:
    """Run the full refund-failure pipeline and commit the artifacts for dashboard offline use.

    Produces a deterministic bundle at apps/dashboard/src/fixtures/refund-failure/ by running
    run-pipeline in a temp dir, then normalizing the run_id to a stable sentinel value so the
    fixture never changes across re-runs (only schema changes should update it).
    """
    import tempfile

    from trace_harness.cli import main as cli_main

    with tempfile.TemporaryDirectory(prefix="trace_fixture_gen_") as tmp:
        runs_dir = Path(tmp) / "runs"
        rc = cli_main(["--runs-dir", str(runs_dir), "run-pipeline", str(DASHBOARD_TASKS_PATH)])
        if rc != 0:
            raise RuntimeError(f"run-pipeline exited {rc}")

        run_dirs = sorted(p for p in runs_dir.iterdir() if p.is_dir())
        if not run_dirs:
            raise RuntimeError("no run directory produced")
        src_dir = run_dirs[0]
        real_run_id = src_dir.name

        DASHBOARD_FIXTURE_DIR.mkdir(parents=True, exist_ok=True)

        for artifact_name in DASHBOARD_ARTIFACTS:
            artifact = src_dir / artifact_name
            if not artifact.is_file():
                raise RuntimeError(f"run-pipeline did not produce {artifact_name}")
            dst = DASHBOARD_FIXTURE_DIR / artifact.name
            dst.write_text(_normalize_artifact(artifact, real_run_id), encoding="utf-8")
            print(f"  [OK] {artifact.name}")

    print(f"[OK] Dashboard fixture -> {DASHBOARD_FIXTURE_DIR}")
    print(f"     run_id normalized to: {FIXTURE_RUN_ID}")


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    passing = build_passing_scenario()
    passing_path = OUTPUT_DIR / "sample_passing_result.json"
    passing_path.write_text(json.dumps(passing, indent=2) + "\n")
    print(f"[OK] Passing result -> {passing_path}")
    print(
        f"     passed={passing['passed']}, "
        f"failed_checks={len(passing['failed_checks'])}, "
        f"warnings={len(passing['warnings'])}"
    )

    failing = build_failing_scenario()
    failing_path = OUTPUT_DIR / "sample_failing_result.json"
    failing_path.write_text(json.dumps(failing, indent=2) + "\n")
    print(f"[OK] Failing result -> {failing_path}")
    print(
        f"     passed={failing['passed']}, "
        f"failed_checks={len(failing['failed_checks'])}, "
        f"warnings={len(failing['warnings'])}"
    )
    print(f"     check_ids: {[c['check_id'] for c in failing['failed_checks']]}")

    generate_dashboard_fixture()


if __name__ == "__main__":
    main()
