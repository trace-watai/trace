"""Check ids, severities and repair templates must move together (#188).

Check ids are string literals scattered through the verifier. The severity map,
the repair-control builders and the materializability map each list a subset by
hand, and nothing noticed when they drifted apart: #176 added three checks and
#139 added two, and until this test existed a run failing on one of them
produced a repair package with no control for it and no warning.

This walks the declared check ids and fails naming exactly what is missing, so
adding a check without a severity or a template fails here rather than silently
producing an incomplete bundle months later.
"""

from __future__ import annotations

from trace_harness.environment.controls import MATERIALIZABLE_REPAIR_CONTROLS
from trace_harness.failure_bundles.generator import _CONTROL_BUILDERS
from trace_harness.verifiers.refund_policy import RefundPolicyVerifier
from trace_harness.verifiers.severity_map import SEVERITY_MAP

CHECK_IDS = RefundPolicyVerifier.CHECK_IDS


def test_declared_check_ids_are_unique() -> None:
    assert len(CHECK_IDS) == len(set(CHECK_IDS))


def test_every_check_id_has_a_severity() -> None:
    missing = sorted(set(CHECK_IDS) - set(SEVERITY_MAP))
    assert not missing, f"check ids with no severity entry: {missing}"


def test_the_severity_map_declares_nothing_the_verifier_cannot_emit() -> None:
    """A severity for a check that no longer exists is dead weight that reads as coverage."""
    stray = sorted(set(SEVERITY_MAP) - set(CHECK_IDS))
    assert not stray, f"severity entries for unknown check ids: {stray}"


def test_every_check_id_has_a_repair_control_template() -> None:
    missing = sorted(set(CHECK_IDS) - set(_CONTROL_BUILDERS))
    assert not missing, f"check ids with no repair control template: {missing}"


def test_every_template_is_listed_in_the_materializability_map() -> None:
    """A prescribed control with no entry cannot be reported as skipped honestly."""
    names = {builder([check]).name for check, builder in _CONTROL_BUILDERS.items()}
    missing = sorted(names - set(MATERIALIZABLE_REPAIR_CONTROLS))
    assert not missing, f"prescribed controls absent from the materializability map: {missing}"


def test_every_template_names_an_installation_point_and_a_behavior() -> None:
    """A template with no seam and no behaviour describes nothing anyone can build."""
    for check, builder in _CONTROL_BUILDERS.items():
        control = builder([check])
        assert control.installation_point.strip(), f"{check}: no installation point"
        assert control.behavior_on_failure.strip(), f"{check}: no behavior_on_failure"
        assert control.linked_verifier_checks, f"{check}: template links no checks"


def test_a_template_links_the_check_it_was_built_for() -> None:
    for check, builder in _CONTROL_BUILDERS.items():
        assert check in builder([check]).linked_verifier_checks


def test_adding_a_check_without_a_template_fails_here() -> None:
    """The guard itself, proved rather than assumed."""
    invented = (*CHECK_IDS, "refund_issued_on_a_tuesday")
    assert sorted(set(invented) - set(_CONTROL_BUILDERS)) == ["refund_issued_on_a_tuesday"]
    assert sorted(set(invented) - set(SEVERITY_MAP)) == ["refund_issued_on_a_tuesday"]
