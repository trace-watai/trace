"""CLI: pipeline stages as subcommands, artifact handoff via disk, exit codes."""

from __future__ import annotations

import json

from conftest import FAILURE_TASK_PATH, VALID_TASK_PATH
from trace_harness.cli import main
from trace_harness.tracing import artifact_store as names


def _only_run_dir(runs_dir):
    run_dirs = [p for p in runs_dir.iterdir() if p.is_dir()]
    assert len(run_dirs) == 1
    return run_dirs[0]


def test_run_pipeline_on_failure_writes_all_artifacts(tmp_path, capsys):
    runs_dir = tmp_path / "runs"
    exit_code = main(["--runs-dir", str(runs_dir), "run-pipeline", str(FAILURE_TASK_PATH)])
    assert exit_code == 0  # finding a failure is success unless gating is requested

    run_dir = _only_run_dir(runs_dir)
    for name in names.ALL_ARTIFACTS:
        assert (run_dir / name).is_file(), f"pipeline did not write {name}"

    verifier = json.loads((run_dir / names.VERIFIER_RESULT).read_text())
    assert verifier["passed"] is False
    out = capsys.readouterr().out
    assert "FAIL" in out
    assert "unauthorized_cash_refund" in out


def test_run_pipeline_fail_on_verifier_gates_with_exit_1(tmp_path):
    exit_code = main(
        [
            "--runs-dir",
            str(tmp_path / "runs"),
            "run-pipeline",
            str(FAILURE_TASK_PATH),
            "--fail-on-verifier",
        ]
    )
    assert exit_code == 1


def test_run_pipeline_on_valid_task_passes_and_skips_bundle(tmp_path, capsys):
    runs_dir = tmp_path / "runs"
    exit_code = main(
        ["--runs-dir", str(runs_dir), "run-pipeline", str(VALID_TASK_PATH), "--fail-on-verifier"]
    )
    assert exit_code == 0

    run_dir = _only_run_dir(runs_dir)
    assert json.loads((run_dir / names.VERIFIER_RESULT).read_text())["passed"] is True
    # No failure artifacts for a passing run — absence is the assertion.
    for name in (names.FAILURE_CARD, names.REPAIR_PACKAGE, names.REGRESSION_ARTIFACT):
        assert not (run_dir / name).exists()
    assert "PASS" in capsys.readouterr().out


def test_stages_run_independently_from_disk(tmp_path):
    """run-fixture, verify, attribute, bundle communicate only via artifacts."""
    runs_dir = tmp_path / "runs"
    assert main(["--runs-dir", str(runs_dir), "run-fixture", str(FAILURE_TASK_PATH)]) == 0
    run_dir = _only_run_dir(runs_dir)
    assert not (run_dir / names.VERIFIER_RESULT).exists()

    assert main(["verify", str(run_dir)]) == 0
    assert (run_dir / names.VERIFIER_RESULT).is_file()

    assert main(["attribute", str(run_dir)]) == 0
    attribution = json.loads((run_dir / names.ATTRIBUTION_RESULT).read_text())
    assert attribution["root_cause_step"] == 3
    assert attribution["first_irreversible_action_step"] == 5

    assert main(["bundle", str(run_dir)]) == 0
    assert (run_dir / names.FAILURE_CARD).is_file()
    assert (run_dir / names.REPAIR_PACKAGE).is_file()
    assert (run_dir / names.REGRESSION_ARTIFACT).is_file()


def test_verify_gate_flag_on_failed_run(tmp_path):
    runs_dir = tmp_path / "runs"
    main(["--runs-dir", str(runs_dir), "run-fixture", str(FAILURE_TASK_PATH)])
    run_dir = _only_run_dir(runs_dir)
    assert main(["verify", str(run_dir), "--fail-on-verifier"]) == 1


def test_runs_dir_flag_works_in_either_position(tmp_path):
    """Users append flags at the end; both positions must work."""
    before, after = tmp_path / "before", tmp_path / "after"
    assert main(["--runs-dir", str(before), "run-fixture", str(VALID_TASK_PATH)]) == 0
    assert main(["run-fixture", str(VALID_TASK_PATH), "--runs-dir", str(after)]) == 0
    # Exactly one run landed in each dir (alongside the runs-dir-level index.json).
    assert len([p for p in before.iterdir() if p.is_dir()]) == 1
    assert len([p for p in after.iterdir() if p.is_dir()]) == 1


def test_input_errors_exit_2_with_clean_message(tmp_path, capsys):
    """Mistyped paths and out-of-order stages must not dump tracebacks."""
    # Nonexistent run directory.
    assert main(["verify", str(tmp_path / "runs" / "run_nope")]) == 2
    assert "error:" in capsys.readouterr().err

    # Nonexistent task fixture.
    assert main(["--runs-dir", str(tmp_path / "runs"), "run-fixture", "no_such_task.json"]) == 2
    assert "error:" in capsys.readouterr().err

    # Pipeline stage run out of order: attribute before verify.
    runs_dir = tmp_path / "runs"
    main(["--runs-dir", str(runs_dir), "run-fixture", str(FAILURE_TASK_PATH)])
    run_dir = _only_run_dir(runs_dir)
    assert main(["attribute", str(run_dir)]) == 2
    err = capsys.readouterr().err
    assert "verifier_result.json" in err  # message names the missing artifact


# --- review-hardening regressions -------------------------------------------


def _task_copy_with(tmp_path, source, **overrides):
    """Write a modified copy of a task fixture with absolute doc/script paths."""
    task = json.loads(source.read_text())
    task["docs_fixture"] = str((source.parent / task["docs_fixture"]).resolve())
    script = task["metadata"].get("fixture_script")
    if script:
        task["metadata"]["fixture_script"] = str((source.parent / script).resolve())
    task.update(overrides)
    path = tmp_path / f"{task['task_id']}_variant.json"
    path.write_text(json.dumps(task))
    return path


def test_missing_fixture_script_is_an_input_error_not_a_gate_trip(tmp_path, capsys):
    """Exit 2 (input error), never 1 — 1 means a verified failure in CI."""
    from conftest import FAILURE_TASK_PATH

    task_path = _task_copy_with(tmp_path, FAILURE_TASK_PATH)
    task = json.loads(task_path.read_text())
    del task["metadata"]["fixture_script"]
    task_path.write_text(json.dumps(task))

    code = main(["--runs-dir", str(tmp_path / "runs"), "run-fixture", str(task_path)])
    assert code == 2
    assert "error:" in capsys.readouterr().err


def test_empty_verifier_ids_is_an_input_error(tmp_path, capsys):
    from conftest import VALID_TASK_PATH

    task_path = _task_copy_with(tmp_path, VALID_TASK_PATH, verifier_ids=[])
    runs_dir = tmp_path / "runs"
    assert main(["--runs-dir", str(runs_dir), "run-fixture", str(task_path)]) == 0
    run_dir = _only_run_dir(runs_dir)
    assert main(["verify", str(run_dir), "--fail-on-verifier"]) == 2
    assert "error:" in capsys.readouterr().err


def test_aborted_run_fails_the_ci_gate_even_with_no_violations(tmp_path, capsys):
    """An agent that does nothing must not be CI-green."""
    from conftest import FAILURE_TASK_PATH

    short_script = tmp_path / "short_script.json"
    short_script.write_text(
        json.dumps(
            {
                "schema_version": "0.1.0",
                "script_id": "short",
                "task_id": "refund_policy_failure",
                "actions": [
                    {
                        "kind": "tool_call",
                        "tool_call": {
                            "tool_name": "search_docs",
                            "arguments": {"query": "refund"},
                        },
                    }
                ],
            }
        )
    )
    task_path = _task_copy_with(tmp_path, FAILURE_TASK_PATH)
    runs_dir = tmp_path / "runs"
    assert (
        main(
            [
                "--runs-dir",
                str(runs_dir),
                "run-fixture",
                str(task_path),
                "--script",
                str(short_script),
            ]
        )
        == 0
    )
    run_dir = _only_run_dir(runs_dir)

    # Without the gate flag: informational, exit 0, but the warning is loud.
    assert main(["verify", str(run_dir)]) == 0
    assert "run did not complete" in capsys.readouterr().out
    # With the gate flag: an incomplete run is a failure, violations or not.
    assert main(["verify", str(run_dir), "--fail-on-verifier"]) == 1


def test_bare_run_id_resolves_under_runs_dir(tmp_path):
    from conftest import VALID_TASK_PATH

    runs_dir = tmp_path / "runs"
    assert main(["--runs-dir", str(runs_dir), "run-fixture", str(VALID_TASK_PATH)]) == 0
    run_id = _only_run_dir(runs_dir).name
    assert main(["--runs-dir", str(runs_dir), "verify", run_id]) == 0
