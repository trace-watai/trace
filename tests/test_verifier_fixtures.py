"""Parametrized execution of verifier fixtures.

Tests the suite of canonical JSON fixtures against the verifier logic,
including specific proofs for no-double-blocking and mutation sensitivity.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trace_harness.tasks.schemas import TaskSpec
from trace_harness.tracing.events import TraceEvent
from trace_harness.verifiers.base import VerifierInput
from trace_harness.verifiers.registry import get_verifier

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "verifier"


def load_verifier_fixtures():
    fixtures = []
    if not FIXTURES_DIR.exists():
        return fixtures
    for p in sorted(FIXTURES_DIR.glob("*.json")):
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
            fixtures.append((p.name, data))
    return fixtures


@pytest.mark.parametrize("name,fixture_data", load_verifier_fixtures())
def test_verifier_fixture_execution(name, fixture_data):
    verifier_id = fixture_data["verifier_id"]
    verifier = get_verifier(verifier_id)
    
    input_data = fixture_data["input"]
    task = TaskSpec.model_validate(input_data["task"])
    trace = [TraceEvent.model_validate(e) for e in input_data["trace"]]
    
    input_obj = VerifierInput.from_parts(
        task=task,
        trace=trace,
        final_state=input_data["final_state"],
        run_id=input_data["run_id"]
    )
    
    result = verifier.verify(input_obj)
    expected = fixture_data["expected"]
    
    actual_check_ids = {c.check_id for c in result.failed_checks}
    assert result.passed is expected["passed"], f"[{name}] Expected passed={expected['passed']}, got {result.passed}. Checks: {actual_check_ids}"
    
    if expected["severity"]:
        assert result.severity is not None
        assert result.severity.value == expected["severity"]
    else:
        assert result.severity is None
        
    assert result.blocks_release is expected["blocks_release"]
    assert actual_check_ids == set(expected["check_ids"])


def test_no_double_blocking_on_stale_policy():
    """Proof that multiple failures from one cause do not double-block."""
    fixture_path = FIXTURES_DIR / "high_stale_policy_reliance.json"
    with open(fixture_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    verifier = get_verifier(data["verifier_id"])
    input_data = data["input"]
    task = TaskSpec.model_validate(input_data["task"])
    trace = [TraceEvent.model_validate(e) for e in input_data["trace"]]
    
    result = verifier.verify(VerifierInput.from_parts(
        task=task, trace=trace, final_state=input_data["final_state"], run_id=input_data["run_id"]
    ))
    
    assert result.passed is False
    assert result.blocks_release is True
    
    # Check that only one of the checks blocks release
    blocking_checks = [c for c in result.failed_checks if c.blocks_release]
    assert len(blocking_checks) == 1
    assert blocking_checks[0].check_id == "unauthorized_store_credit"
    
    non_blocking = [c for c in result.failed_checks if not c.blocks_release]
    assert len(non_blocking) == 1
    assert non_blocking[0].check_id == "deprecated_policy_treated_as_authoritative"


def test_mutation_flips_valid_cash_to_failure():
    """Mutating a valid fixture out-of-bounds must trigger the verifier."""
    fixture_path = FIXTURES_DIR / "pass_allowed_cash_refund.json"
    with open(fixture_path, "r", encoding="utf-8") as f:
        data = json.load(f)
        
    # Mutate age to 47 (outside cash window)
    data["input"]["final_state"]["orders"][0]["purchase_age_days"] = 47
    
    verifier = get_verifier(data["verifier_id"])
    input_data = data["input"]
    task = TaskSpec.model_validate(input_data["task"])
    trace = [TraceEvent.model_validate(e) for e in input_data["trace"]]
    
    result = verifier.verify(VerifierInput.from_parts(
        task=task, trace=trace, final_state=input_data["final_state"], run_id=input_data["run_id"]
    ))
    
    assert result.passed is False
    check_ids = {c.check_id for c in result.failed_checks}
    assert "unauthorized_cash_refund" in check_ids
    assert result.severity is not None
    assert result.severity.value == "critical"
    assert result.blocks_release is True
