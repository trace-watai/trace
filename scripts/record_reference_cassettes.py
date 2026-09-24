"""Record a reference agent's cassettes from the tasks' fixture scripts.

Record into a new directory (recording never overwrites), then compare with or
copy into fixtures/cassettes/. The script runs from any working directory.

    python scripts/record_reference_cassettes.py langgraph_ref --root /tmp/cassettes

The model is scripted, so the recording holds the fixture script's turns and
the requests the reference agent sent for them. No network call is made.

Exits 1 when any task's run does not complete with a final answer (a root that
already holds the cassette, for example). A file left under the root by such a
run is partial and is not a recording.
"""

from __future__ import annotations

import argparse
import importlib
import sys
import tempfile
from pathlib import Path

from trace_harness.agents.turns import record_cassette
from trace_harness.runner.result import RunStatus

REPO_ROOT = Path(__file__).resolve().parents[1]
TASKS = [
    REPO_ROOT / "fixtures" / "tasks" / "refund_policy_valid_cash.json",
    REPO_ROOT / "fixtures" / "tasks" / "refund_policy_failure.json",
]
AGENTS = {
    "langgraph_ref": "LangGraphReferenceAgent",
    "openai_agents_ref": "OpenAIAgentsReferenceAgent",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("agent", choices=sorted(AGENTS))
    parser.add_argument("--root", type=Path, required=True, help="cassette root to record into")
    parser.add_argument("tasks", nargs="*", type=Path, default=TASKS, help="task fixtures")
    args = parser.parse_intermixed_args(argv)
    module = importlib.import_module(f"trace_harness.agents.{args.agent}")
    make_agent = getattr(module, AGENTS[args.agent])
    incomplete: list[str] = []
    with tempfile.TemporaryDirectory() as runs:
        for task in args.tasks:
            result = record_cassette(
                make_agent, task, namespace=module.NAMESPACE, root=args.root, runs_dir=Path(runs)
            )
            print(f"{task}: {result.status.value} ({result.termination_reason.value})")
            if result.status is not RunStatus.COMPLETED:
                incomplete.append(f"{task}: {result.error or result.termination_reason.value}")
    if incomplete:
        print("error: these tasks were not recorded completely:", file=sys.stderr)
        for line in incomplete:
            print(f"  {line}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
