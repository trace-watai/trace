"""Estimate what exp_001's live arms cost against the plan's cap, before any key is used.

Token counts come from the one retained Gemini cassette with usage,
fixtures/cassettes/refund_policy_failure/gemini-3.6-flash/default.jsonl, a live
gemini-3.6-flash run of refund_policy_failure recorded under #144. Prices come
from the adapters' own tables through ``estimate_cost_usd``, so a price change
in code changes this estimate.

A branched run's call at step ``s`` sends the transcript up to ``s``, so its
input is priced at the cassette's input count for step ``s``. Past the
cassette's last step, input grows by the largest step-to-step increase the
cassette shows. Every call is priced at the largest output count the cassette
shows, thinking included, or at ``--output-tokens`` when given. A run's calls
are priced together, rounded to the micro-dollar as the harness rounds a run's
cost.

Two scenarios per live condition.

- expected: five seeds, each making as many calls after the fork as the
  recording did.
- high: ten runs (seeds 0 to 4 and all five replacements), each running to
  the step limit.

Neither is a bound. A call can return more output tokens than the cassette's
largest, and a live transcript can grow faster than the recorded one. What
bounds the spend is the plan's cap, which the budget guard checks between runs.

The swapped model is priced on Gemini's token counts, since no Claude run is
retained. Anthropic counts tokens with its own tokenizer, so that line is a
proxy.

Usage::

    python scripts/estimate_exp_001_cost.py [--plan PATH] [--cassette PATH]
        [--output-tokens N]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLAN = REPO / "docs/acceptance/experiments/exp_001_replay_validity/experiment.json"
CASSETTE = REPO / "fixtures/cassettes/refund_policy_failure/gemini-3.6-flash/default.jsonl"
LIVE_ARMS = ("live", "live_no_control", "live_swapped")


def cassette_tokens(path: Path) -> tuple[list[int], int]:
    """Input tokens per step, and the largest output of any step, from a cassette."""
    inputs, outputs = [], []
    for line in path.read_text(encoding="utf-8").splitlines():
        usage = json.loads(line)["usage"]
        inputs.append(usage["prompt_token_count"])
        outputs.append(usage["candidates_token_count"] + usage.get("thoughts_token_count", 0))
    return inputs, max(outputs)


def input_at(step: int, inputs: list[int]) -> int:
    if step <= len(inputs):
        return inputs[step - 1]
    growth = max(b - a for a, b in zip(inputs, inputs[1:], strict=False))
    return inputs[-1] + (step - len(inputs)) * growth


def run_cost(provider: str, model: str, calls: list[tuple[int, int]]) -> float:
    """One run's calls priced together, as the harness prices a run."""
    from trace_harness.models import estimate_cost_usd

    raws = [
        {"usage_metadata": {"prompt_token_count": got_in, "candidates_token_count": got_out}}
        if provider == "gemini"
        else {"usage": {"input_tokens": got_in, "output_tokens": got_out}}
        for got_in, got_out in calls
    ]
    cost = estimate_cost_usd(provider, model, raws)
    if cost is None:
        raise ValueError(f"{provider} {model} has no price, so the cap cannot hold")
    return cost


def estimate(
    plan_path: Path = PLAN, cassette: Path = CASSETTE, output_tokens: int | None = None
) -> dict:
    """Per-condition and total dollars under both scenarios."""
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    inputs, largest = cassette_tokens(cassette)
    output = largest if output_tokens is None else output_tokens
    artifacts = {fp["source_run_id"]: fp["artifact"] for fp in plan["metadata"]["fork_points"]}
    replacements = len(plan["metadata"].get("replacement_seeds") or [])
    rows = []
    for condition in plan["conditions"]:
        if condition["kind"] not in LIVE_ARMS:
            continue
        agent = condition["agent_config"]
        fork = condition["start"]["step_id"]
        artifact = json.loads((REPO / artifacts[condition["start"]["source_run_id"]]).read_text())
        recorded_after = len(artifact["pinned_agent_actions"]) - fork
        max_steps = agent.get("max_steps", 16)

        def one_run(calls: int, fork: int = fork, agent: dict = agent) -> float:
            steps = range(fork + 1, fork + 1 + calls)
            priced = [(input_at(s, inputs), output) for s in steps]
            return run_cost(agent["provider"], agent["model"], priced)

        seeds = len(condition["seeds"])
        rows.append(
            {
                "condition": condition["name"],
                "model": agent["model"],
                "expected_usd": seeds * one_run(recorded_after),
                "high_usd": (seeds + replacements) * one_run(max_steps - fork),
            }
        )
    cap = plan["budget"]["max_cost_usd"]
    return {
        "cap_usd": cap,
        "rows": rows,
        "expected_usd": round(sum(r["expected_usd"] for r in rows), 2),
        "high_usd": round(sum(r["high_usd"] for r in rows), 2),
        "cassette_inputs": inputs,
        "cassette_max_output": largest,
        "output_tokens_per_call": output,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plan", type=Path, default=PLAN)
    parser.add_argument("--cassette", type=Path, default=CASSETTE)
    parser.add_argument(
        "--output-tokens",
        type=int,
        default=None,
        help="price every call at this many output tokens instead of the cassette's largest",
    )
    args = parser.parse_args(argv)
    result = estimate(args.plan, args.cassette, args.output_tokens)
    print(f"input tokens per step from the cassette: {result['cassette_inputs']}")
    print(f"output tokens per call: {result['output_tokens_per_call']}")
    print(f"{'condition':<64} {'model':<18} {'expected':>9} {'high':>9}")
    for row in result["rows"]:
        print(
            f"{row['condition']:<64} {row['model']:<18} "
            f"{row['expected_usd']:>9.4f} {row['high_usd']:>9.4f}"
        )
    print(
        f"total: expected ${result['expected_usd']:.2f}, high ${result['high_usd']:.2f}, "
        f"cap ${result['cap_usd']:.2f}"
    )
    return 0 if result["high_usd"] <= result["cap_usd"] else 1


if __name__ == "__main__":
    sys.exit(main())
