"""Record a reference agent's cassettes from the tasks' fixture scripts.

Run from the repository root, into a new directory (recording never
overwrites), then compare with or copy into fixtures/cassettes/.

    python scripts/record_reference_cassettes.py langgraph_ref --root /tmp/cassettes

The model is scripted, so the recording holds the fixture script's turns and
the requests the reference agent sent for them. No network call is made.
"""

from __future__ import annotations

import argparse
import importlib
import tempfile
from pathlib import Path

from trace_harness.agents.turns import record_cassette

TASKS = [
    "fixtures/tasks/refund_policy_valid_cash.json",
    "fixtures/tasks/refund_policy_failure.json",
]
AGENTS = {"langgraph_ref": "LangGraphReferenceAgent"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("agent", choices=sorted(AGENTS))
    parser.add_argument("--root", type=Path, required=True, help="cassette root to record into")
    parser.add_argument("tasks", nargs="*", default=TASKS, help="task fixtures to record")
    args = parser.parse_args()
    module = importlib.import_module(f"trace_harness.agents.{args.agent}")
    make_agent = getattr(module, AGENTS[args.agent])
    with tempfile.TemporaryDirectory() as runs:
        for task in args.tasks:
            result = record_cassette(
                make_agent, task, namespace=module.NAMESPACE, root=args.root, runs_dir=Path(runs)
            )
            print(f"{task}: {result.status.value} ({result.termination_reason.value})")


if __name__ == "__main__":
    main()
