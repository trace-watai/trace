"""CLI entry point: ``python -m AgentRunner --task refund_001``."""

from __future__ import annotations

import argparse
import os
import sys

from .runner import RunConfig, run
from .taskspec import load_by_id
from .trace import ListTrace

_DEFAULT_TASKS_DIR = os.path.join(os.path.dirname(__file__), "tasks")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="AgentRunner")
    parser.add_argument("--task", required=True, help="task id (file: {id}.json)")
    parser.add_argument("--provider", default="fixture")
    parser.add_argument("--model", default="none")
    parser.add_argument("--max-steps", type=int, default=8)
    parser.add_argument("--tasks-dir", default=_DEFAULT_TASKS_DIR)
    parser.add_argument("--quiet", action="store_true", help="suppress per-step trace")
    args = parser.parse_args(argv)

    try:
        task = load_by_id(args.task, args.tasks_dir)
    except FileNotFoundError:
        print(f"error: task {args.task!r} not found in {args.tasks_dir!r}", file=sys.stderr)
        return 2

    config = RunConfig(
        task_id=task.task_id,
        provider=args.provider,
        model=args.model,
        max_steps=args.max_steps,
    )

    trace = ListTrace()
    result = run(task, config, trace=trace)

    if not args.quiet:
        for e in trace.events:
            print(f"[step {e['step']:>2}] {e['kind']:<13} {e['payload']}")
        print()

    print(f"run_id          : {result.run_id}")
    print(f"task_id         : {result.task_id}")
    print(f"termination     : {result.termination_reason}")
    print(f"steps_taken     : {result.steps_taken}")
    print(f"events_emitted  : {len(trace.events)}")
    print(f"final_output    : {result.final_output!r}")
    if result.error:
        print(f"error           : {result.error}")

    return 1 if result.termination_reason == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
