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
    assert len(list(before.iterdir())) == 1
    assert len(list(after.iterdir())) == 1


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
