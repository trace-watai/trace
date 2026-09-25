"""A stand-in for the ``claude`` CLI, so the Claude Code agent is tested without it.

``tests/test_claude_code_agent.py`` puts a ``claude`` wrapper around this file
first on PATH. It behaves like ``claude -p --output-format stream-json``
closely enough to exercise the agent: it reads the prompt from stdin, starts the
MCP server named in ``--mcp-config`` the way the CLI does, lists its tools,
emits a ``system/init`` message, then plays a scenario. Each turn is written as
assistant messages sharing one message id, one content block each, and every
tool call is made over MCP, so the agent's relay, the bridge and the harness
runner handle it for real. The run ends with a ``result`` message carrying
usage and ``total_cost_usd``.

The scenario is a JSON file named by ``FAKE_CLAUDE_SCENARIO``. ``script`` plays
a task's fixture script, with each action's reasoning as text beside its tool
call. ``turns`` gives the turns directly. Other keys switch on a failure mode.
What the fake saw (its arguments, the prompt, which environment variables were
set, the tool list and every tool result) is written to ``record``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from typing import Any

MODEL_USAGE = {"input_tokens": 1200, "output_tokens": 80, "cache_read_input_tokens": 0}
WATCHED_VARIABLES = (
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ENABLE_TOOL_SEARCH",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY",
)


def emit(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def option(argv: list[str], flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv else None


class McpClient:
    """Just enough of an MCP client to drive the harness's stdio server."""

    def __init__(self, config_path: str) -> None:
        with open(config_path, encoding="utf-8") as handle:
            server = json.load(handle)["mcpServers"]["trace"]
        self.process = subprocess.Popen(
            [server["command"], *server["args"]],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        self._next_id = 0
        self._lock = threading.Lock()
        self._replies: dict[int, dict[str, Any]] = {}
        self._arrived = threading.Condition()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            message = json.loads(line)
            with self._arrived:
                self._replies[message["id"]] = message
                self._arrived.notify_all()

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            self._next_id += 1
            message_id = self._next_id
            assert self.process.stdin is not None
            payload = {"jsonrpc": "2.0", "id": message_id, "method": method, "params": params or {}}
            self.process.stdin.write(json.dumps(payload) + "\n")
            self.process.stdin.flush()
        with self._arrived:
            while message_id not in self._replies:
                self._arrived.wait()
            return self._replies.pop(message_id)

    def notify(self, method: str) -> None:
        with self._lock:
            assert self.process.stdin is not None
            self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
            self.process.stdin.flush()

    def close(self) -> None:
        self.process.terminate()
        self.process.wait(5)


def turns_from_script(path: str) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as handle:
        actions = json.load(handle)["actions"]
    turns: list[dict[str, Any]] = []
    for action in actions:
        if action["kind"] == "final_answer":
            turns.append({"thinking": action.get("reasoning"), "final": action["final_answer"]})
        else:
            call = action["tool_call"]
            turns.append(
                {
                    "text": action.get("reasoning"),
                    "tools": [{"name": call["tool_name"], "input": call["arguments"]}],
                }
            )
    return turns


def main() -> int:
    argv = sys.argv[1:]
    with open(os.environ["FAKE_CLAUDE_SCENARIO"], encoding="utf-8") as handle:
        scenario = json.load(handle)
    prompt = sys.stdin.read()
    record: dict[str, Any] = {
        "argv": argv,
        "prompt": prompt,
        "set_variables": sorted(name for name in WATCHED_VARIABLES if name in os.environ),
        "tool_search": os.environ.get("ENABLE_TOOL_SEARCH"),
        "cwd": os.getcwd(),
        "pid": os.getpid(),
        "results": [],
    }

    def save() -> None:
        with open(scenario["record"], "w", encoding="utf-8") as handle:
            json.dump(record, handle)

    save()
    if scenario.get("exit_before_init") is not None:
        sys.stderr.write("fake claude: something went wrong before the run\n")
        return int(scenario["exit_before_init"])

    client = McpClient(option(argv, "--mcp-config") or "")
    record["mcp_pid"] = client.process.pid
    initialized = client.request("initialize", {"protocolVersion": "2025-06-18"})
    record["protocol"] = initialized["result"]["protocolVersion"]
    client.notify("notifications/initialized")
    listed = client.request("tools/list")["result"]["tools"]
    record["listed_tools"] = listed
    save()

    model = option(argv, "--model") or "default"
    tools = [f"mcp__trace__{tool['name']}" for tool in listed]
    emit(
        {
            "type": "system",
            "subtype": "init",
            "session_id": "fake-session",
            "uuid": "fake-init",
            "cwd": os.getcwd(),
            "model": scenario.get("init_model", model),
            "tools": tools + scenario.get("extra_tools", []),
            "mcp_servers": [{"name": "trace", "status": scenario.get("mcp_status", "connected")}],
            "apiKeySource": scenario.get("api_key_source", "none"),
            "permissionMode": option(argv, "--permission-mode"),
            "claude_code_version": "0.0.0-fake",
        }
    )
    if scenario.get("malformed"):
        sys.stdout.write("this line is not json\n")
        sys.stdout.flush()
    if scenario.get("hang_seconds"):
        time.sleep(float(scenario["hang_seconds"]))
    if scenario.get("rate_limited"):
        emit(
            {
                "type": "rate_limit_event",
                "rate_limit_info": {"status": "rejected", "resetsAt": 1790000000},
                "uuid": "fake-rate",
                "session_id": "fake-session",
            }
        )
        time.sleep(60)

    if "script" in scenario:
        turns = turns_from_script(scenario["script"])
    else:
        turns = scenario.get("turns", [])
    answered_by = scenario.get("answer_model", model)
    final = ""
    for number, turn in enumerate(turns, start=1):
        message_id = f"msg_fake_{number}"

        def block(content: dict[str, Any], message_id: str = message_id) -> None:
            emit(
                {
                    "type": "assistant",
                    "uuid": f"u-{message_id}-{content['type']}",
                    "session_id": "fake-session",
                    "parent_tool_use_id": None,
                    "message": {
                        "id": message_id,
                        "type": "message",
                        "role": "assistant",
                        "model": answered_by,
                        "content": [content],
                        "stop_reason": None,
                        "usage": MODEL_USAGE,
                    },
                }
            )

        if turn.get("thinking"):
            block({"type": "thinking", "thinking": turn["thinking"], "signature": "c2lnbmF0dXJl"})
        if "final" in turn:
            final = turn["final"]
            block({"type": "text", "text": final})
            break
        if turn.get("text"):
            block({"type": "text", "text": turn["text"]})
        calls = turn["tools"]
        late = float(scenario.get("late_tool_use_seconds") or 0)

        def announce(calls: list[dict[str, Any]] = calls, number: int = number) -> None:
            for index, call in enumerate(calls):
                block(
                    {
                        "type": "tool_use",
                        "id": f"toolu_{number}_{index}",
                        "name": f"mcp__trace__{call['name']}",
                        "input": call["input"],
                    }
                )

        if not late:
            announce()
        results: list[Any] = [None] * len(calls)

        def run_call(index: int, call: dict[str, Any], results: list[Any] = results) -> None:
            params = {"name": call["name"], "arguments": call["input"]}
            results[index] = client.request("tools/call", params)["result"]

        threads = [
            threading.Thread(target=run_call, args=(index, call))
            for index, call in enumerate(calls)
        ]
        for thread in threads:
            thread.start()
        if late:
            # The call reaches the MCP server before its block reaches stdout,
            # which the two pipes allow.
            time.sleep(late)
            announce()
        for thread in threads:
            thread.join()
        for index, result in enumerate(results):
            record["results"].append({"tool": calls[index]["name"], **result})
            emit(
                {
                    "type": "user",
                    "session_id": "fake-session",
                    "parent_tool_use_id": None,
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": f"toolu_{number}_{index}",
                                "content": result["content"],
                                "is_error": result["isError"],
                            }
                        ],
                    },
                }
            )
        save()
    save()
    if scenario.get("linger_seconds"):
        # A CLI still busy after its last tool call, as one that keeps thinking.
        time.sleep(float(scenario["linger_seconds"]))
    client.close()

    if scenario.get("exit_without_result") is not None:
        return int(scenario["exit_without_result"])
    error = scenario.get("error_result")
    if scenario.get("assistant_error"):
        # How the CLI reports a failed API call, such as a missing login.
        emit(
            {
                "type": "assistant",
                "error": scenario["assistant_error"],
                "uuid": "fake-synthetic",
                "session_id": "fake-session",
                "parent_tool_use_id": None,
                "message": {
                    "id": "msg_synthetic",
                    "model": "<synthetic>",
                    "role": "assistant",
                    "content": [{"type": "text", "text": error or ""}],
                },
            }
        )
    usage = {"input_tokens": 4800, "output_tokens": 320, "cache_read_input_tokens": 0}
    emit(
        {
            "type": "result",
            "subtype": "success" if error is None else "error_during_execution",
            "uuid": "fake-result",
            "session_id": "fake-session",
            "is_error": error is not None,
            "duration_ms": 1234,
            "duration_api_ms": 1000,
            "num_turns": len(turns),
            "result": final if error is None else error,
            "stop_reason": "end_turn",
            "total_cost_usd": scenario.get("total_cost_usd", 0.0123),
            "usage": usage,
            "modelUsage": {model: {"inputTokens": 4800, "outputTokens": 320, "costUSD": 0.0123}},
            "permission_denials": [],
        }
    )
    return 1 if error is not None else 0


if __name__ == "__main__":
    sys.exit(main())
