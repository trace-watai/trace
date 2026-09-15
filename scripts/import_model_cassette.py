#!/usr/bin/env python3
"""Convert a completed retained trace to a cassette without calling a provider.

Historical traces contain prompt deltas and normalized actions, but not complete
tool declarations. Those declarations are reconstructed from the current tool
registry; replay's request checks must pass before treating the import as valid.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trace_harness.environment.support_env import SupportEnvironment
from trace_harness.models.base import AgentAction, Message
from trace_harness.models.cassette import (
    CassetteEntry,
    CassetteError,
    CassetteRequestConfig,
    cassette_path,
    fingerprint,
    safe_response,
)
from trace_harness.runner.config import RunConfig
from trace_harness.tasks.schemas import TaskSpec


def import_cassette(run_dir: Path, directory: Path) -> Path:
    def read(name: str) -> dict:
        return json.loads((run_dir / name).read_text(encoding="utf-8"))

    if read("run_result.json")["status"] != "completed":
        raise CassetteError("only completed retained runs can be imported")
    run_config = RunConfig.model_validate(read("run_config.json"))
    if run_config.model is None:
        raise CassetteError("retained run must identify its model explicitly")
    config = CassetteRequestConfig(
        **run_config.model_dump(include=set(CassetteRequestConfig.model_fields))
    )
    task = TaskSpec.model_validate(read("task_spec.json"))
    if task.task_id != config.task_id:
        raise CassetteError("retained task and config disagree")
    tools = SupportEnvironment.from_task(task).tool_specs()
    tools_hash = fingerprint([tool.model_dump(mode="json") for tool in tools])
    transcript: list[Message] = []
    entries: list[CassetteEntry] = []
    pending_step = None
    raw = None
    for line in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines():
        event = json.loads(line)  # strict: never drop a malformed trailing line
        kind, step, payload = event["event_type"], event["step_id"], event["payload"]
        if kind == "model_prompt":
            if pending_step is not None or step != len(entries) + 1:
                raise CassetteError("retained prompt sequence is incomplete")
            transcript.extend(Message.model_validate(m) for m in payload["new_messages"])
            if len(transcript) != payload["transcript_length"]:
                raise CassetteError("retained prompt delta is incomplete")
            pending_step, raw = step, None
        elif kind == "model_response":
            if step != pending_step or raw is not None:
                raise CassetteError("retained response has no matching prompt")
            raw = payload["raw"]
        elif kind == "model_action":
            if pending_step is None or step != pending_step:
                raise CassetteError("retained action has no matching prompt")
            response, usage = safe_response(AgentAction.model_validate({**payload, "raw": raw}))
            entries.append(
                CassetteEntry(
                    cassette_id=fingerprint(config.model_dump(mode="json")),
                    config=config,
                    step=step,
                    transcript_hash=fingerprint([m.model_dump(mode="json") for m in transcript]),
                    tools_hash=tools_hash,
                    response=response,
                    usage=usage,
                )
            )
            pending_step = None
    if pending_step is not None or not entries or entries[-1].response["kind"] != "final_answer":
        raise CassetteError("retained trace is missing its final action")
    path = cassette_path(directory, config)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as output:
        for entry in entries:
            output.write(entry.model_dump_json() + "\n")
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--cassette-dir", type=Path, default=Path("fixtures/cassettes"))
    args = parser.parse_args()
    print(import_cassette(args.run_dir, args.cassette_dir))
