"""The validate-fixtures gate (#185).

Three docs said this command enforced task validity. It did not exist, the
checker globbed the top level only, and nothing in the repo check called it, so
a task under refund_task_families that no suite referenced could merge without
ever being validated.
"""

from __future__ import annotations

from conftest import FIXTURES_DIR

# --- the validate-fixtures gate (#185) ---


def test_every_committed_task_fixture_is_valid() -> None:
    """The a1 number, enforced rather than reported."""
    from trace_harness.tasks.validation import validate_fixture_tree

    verdicts = validate_fixture_tree(FIXTURES_DIR / "tasks")
    bad = [str(v.path.name) for v in verdicts if not v.ok]

    assert verdicts, "no task fixtures found"
    assert not bad, f"invalid task fixtures: {bad}"


def test_the_tree_walk_reaches_the_family_tasks() -> None:
    """The old entry point globbed the top level, so family tasks went unchecked."""
    from trace_harness.tasks.validation import collect_task_files

    found = collect_task_files(FIXTURES_DIR / "tasks")
    assert any("refund_task_families" in str(p) for p in found)
    assert len(found) > 30


def test_generated_candidates_are_excluded(tmp_path) -> None:
    """#14's candidates are not authored fixtures, so the gate skips them."""
    from trace_harness.tasks.validation import collect_task_files

    root = tmp_path / "tasks"
    (root / "candidates").mkdir(parents=True)
    (root / "a.json").write_text("{}", encoding="utf-8")
    (root / "candidates" / "b.json").write_text("{}", encoding="utf-8")

    assert [p.name for p in collect_task_files(root)] == ["a.json"]


def test_a_counterexample_that_stops_being_flagged_fails(tmp_path) -> None:
    from trace_harness.tasks.validation import validate_fixture_tree

    root = tmp_path / "tasks"
    (root / "counterexamples").mkdir(parents=True)
    valid = (FIXTURES_DIR / "tasks" / "refund_policy_valid_cash.json").read_text()
    (root / "counterexamples" / "not_broken.json").write_text(valid, encoding="utf-8")

    (verdict,) = validate_fixture_tree(root)
    assert verdict.is_counterexample
    assert not verdict.ok


def test_the_command_exits_one_on_a_problem(tmp_path, capsys) -> None:
    import json

    from trace_harness.cli import main

    root = tmp_path / "tasks"
    root.mkdir()
    task = json.loads((FIXTURES_DIR / "tasks" / "refund_policy_valid_cash.json").read_text())
    task["verifier_ids"] = []
    (root / "broken.json").write_text(json.dumps(task), encoding="utf-8")

    assert main(["validate-fixtures", str(root)]) == 1
    out = capsys.readouterr().out
    assert "broken.json" in out
    assert "empty_verifier_ids" in out
