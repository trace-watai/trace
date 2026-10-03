"""Rehearse exp_001 offline with the fixture model standing in for every live model.

This is pre-registration 001's harness check, run over the whole plan. The
fixture model plays each fork point's recorded continuation, so its live
verdicts must equal the static replay verdicts on all three pairs with zero
divergence. Anything else is a harness defect, and the pre-registration stops
runs until it is fixed.

The rehearsal follows the runbook (docs/experiments/runbook_001.md) step for
step, in a scratch folder and never in the retained one:

1. write a copy of the plan with every live agent config replaced by the
   fixture provider and ``_dry_run`` appended to the experiment id, and
   freeze it when the plan is not frozen yet
2. ``branch`` each condition from its fork point's artifact, static replay
   first, then live, live_no_control and live_swapped
3. ``experiment record`` every batch with decision review by human
4. ``scripts/retain_exp_001.sh`` into a scratch retained folder
5. ``scripts/regenerate_exp_001.sh`` on that folder, which must reproduce
   result.json and repair_effectiveness.json exactly, timestamps excluded

It exits 0 when the harness check passes, all eight metrics are non-null and
the regeneration matches, and 1 otherwise.

On a frozen plan, which is runbook step 3, the outcome is retained as
``harness_check.json`` beside the plan, pass or fail, with the plan's sha256
and frozen set, so the committed experiment folder shows which plan the check
ran on and what it found. An unfrozen plan's rehearsal is a preview and is not
retained. ``--no-retain`` skips the file on a frozen plan too.

Usage::

    python scripts/dry_run_exp_001.py [--plan PATH] [--work DIR] [--no-retain]
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLAN = "docs/acceptance/experiments/exp_001_replay_validity/experiment.json"
ARMS = ("static_replay", "live", "live_no_control", "live_swapped")
LIVE_ARMS = frozenset(ARMS[1:])
#: The retained outcome of the harness check, written beside a frozen plan.
HARNESS_CHECK = "harness_check.json"


def stand_in(plan: dict) -> dict:
    """The plan with the fixture model answering every live condition.

    Returned as the plan model dumps it, every default filled in, which is how
    ``experiment record`` writes the plan it read. The retain script compares
    the two, so a stand-in written any other way fails retention.
    """
    from trace_harness.runner.experiment import ExperimentSpec

    copy = json.loads(json.dumps(plan))
    copy["experiment_id"] = f"{plan['experiment_id']}_dry_run"
    for condition in copy["conditions"]:
        if condition["kind"] in LIVE_ARMS:
            label = condition["agent_config"]["label"]
            condition["agent_config"] = {"label": f"fixture-for-{label}", "provider": "fixture"}
    return ExperimentSpec.model_validate(copy).model_dump(mode="json")


def artifact_of(plan: dict, condition: dict) -> str:
    by_run = {fp["source_run_id"]: fp["artifact"] for fp in plan["metadata"]["fork_points"]}
    return by_run[condition["start"]["source_run_id"]]


def cli(argv: list[str]) -> tuple[int, str]:
    from trace_harness.cli import main

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(argv)
    return code, out.getvalue()


def harness_check(result: dict) -> list[str]:
    """Why the rehearsal fails the pre-registration's harness check; empty when it passes."""
    problems = []
    metrics = result["metrics"]
    for name, value in metrics.items():
        if name != "extra" and value is None:
            problems.append(f"{name} is null")
    pairs = result["metadata"].get("verdict_agreement_pairs") or []
    if len({(p["kind"], p["artifact"]) for p in pairs}) != len(pairs) or not pairs:
        problems.append("no pairs, or a pair counted twice")
    for pair in pairs:
        if pair["excluded"] or not pair["agrees"]:
            problems.append(
                f"{pair['kind']} {pair['task_id']}: static clear {pair['static_clear']}, "
                f"{pair['clear_seeds']}/{pair['completed_seeds']} live seeds clear, "
                f"excluded {pair['excluded']}"
            )
    for name in ("first_post_fork_divergence_rate", "noise_floor_divergence_rate"):
        if metrics[name] not in (None, 0.0):
            problems.append(f"{name} is {metrics[name]}, the fixture model must not diverge")
    return problems


def rehearse(plan: dict, work: Path) -> tuple[list[str], dict | None, dict | None]:
    """Run steps 1 to 5 in ``work``; return the problems found, the result and the sidecar.

    A step that exits non-zero ends the rehearsal there, as one problem, with
    no result or sidecar.
    """
    runs, retained = work / "runs", work / "retained"
    retained.mkdir(parents=True)
    plan_path = retained / "experiment.json"
    plan_path.write_text(json.dumps(stand_in(plan), indent=2) + "\n", encoding="utf-8")
    if plan["frozen_manifest"].get("frozen_set") is None:
        code, out = cli(["experiment", "freeze", str(plan_path)])
        if code:
            print(out)
            return [f"experiment freeze of the stand-in plan exited {code}"], None, None

    pairs: list[str] = []
    order = sorted(plan["conditions"], key=lambda c: ARMS.index(c["kind"]))
    for condition in order:
        branch = ["--runs-dir", str(runs), "branch", artifact_of(plan, condition)]
        branch += ["--experiment", str(plan_path), "--condition", condition["name"]]
        code, out = cli(branch)
        found = re.findall(r"--condition (\S+=batch_\S+)", out)
        print(f"branch {condition['name']}: exit {code}, {' '.join(found) or 'no batch'}")
        if code:
            print(out)
            return [f"branch {condition['name']} exited {code}"], None, None
        pairs += found

    record = ["--runs-dir", str(runs), "experiment", "record", str(plan_path)]
    record += ["--decision", "review", "--decided-by", "human"]
    for pair in pairs:
        record += ["--condition", pair]
    code, out = cli(record)
    print(out)
    if code:
        return [f"experiment record exited {code}"], None, None

    env = {**os.environ, "PYTHON": sys.executable}
    sys.stdout.flush()
    retain = ["bash", "scripts/retain_exp_001.sh", str(runs), str(retained)]
    code = subprocess.run(retain, env=env).returncode
    if code:
        return [f"retain_exp_001.sh exited {code}"], None, None
    regenerated = subprocess.run(["bash", "scripts/regenerate_exp_001.sh", str(retained)], env=env)

    result = json.loads((retained / "result.json").read_text(encoding="utf-8"))
    sidecar = json.loads((retained / "repair_effectiveness.json").read_text(encoding="utf-8"))
    for entry in sidecar["entries"]:
        on, off = entry["control_on"], entry["control_off"]
        value = entry["repair_effectiveness"]
        print(
            f"B1 {entry['control_on']['condition']}: on "
            f"{on['blocking_failures_after_fork']}/{on['completed_runs']}, off "
            f"{off['blocking_failures_after_fork']}/{off['completed_runs']}, "
            f"{value if entry['null_reason'] is None else 'null, ' + entry['null_reason']}"
        )
    problems = harness_check(result)
    if regenerated.returncode:
        problems.append(f"regenerate_exp_001.sh exited {regenerated.returncode}")
    return problems, result, sidecar


def retain_check(
    plan_file: Path, problems: list[str], result: dict | None, sidecar: dict | None
) -> Path:
    """Write ``harness_check.json`` beside a frozen plan and return its path."""
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    frozen_set = plan["frozen_manifest"]["frozen_set"]
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=REPO)
    record = {
        "experiment_id": plan["experiment_id"],
        "plan_sha256": hashlib.sha256(plan_file.read_bytes()).hexdigest(),
        "frozen_set": {name: component["digest"] for name, component in frozen_set.items()},
        "commit": head.stdout.strip() if head.returncode == 0 else None,
        "checked_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "passed": not problems,
        "problems": problems,
        "metrics": result["metrics"] if result else None,
        "verdict_agreement_pairs": (
            result["metadata"].get("verdict_agreement_pairs") if result else None
        ),
        "repair_effectiveness": sidecar["entries"] if sidecar else None,
    }
    path = plan_file.parent / HARNESS_CHECK
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plan", default=PLAN, help="the exp_001 plan to rehearse")
    parser.add_argument("--work", default=None, help="scratch folder, kept afterwards")
    parser.add_argument(
        "--no-retain",
        action="store_true",
        help=f"do not write {HARNESS_CHECK} beside a frozen plan",
    )
    args = parser.parse_args(argv)
    os.chdir(REPO)

    work = Path(args.work or tempfile.mkdtemp(prefix="exp_001_dry_run_")).resolve()
    plan_file = Path(args.plan).resolve()
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    problems, result, sidecar = rehearse(plan, work)

    for problem in problems:
        print(f"harness check: {problem}")
    print(f"harness check: {'FAIL' if problems else 'PASS'}  (scratch folder {work})")
    if plan["frozen_manifest"].get("frozen_set") is None:
        print("harness check: not retained, since the plan is not frozen")
    elif not args.no_retain:
        print(f"harness check: retained in {retain_check(plan_file, problems, result, sidecar)}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
