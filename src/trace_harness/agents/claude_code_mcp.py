"""The MCP server that gives the Claude Code CLI exactly the task's tools.

The Claude Code reference agent (``claude_code_ref.py``) names this file in the
``--mcp-config`` it hands the CLI, and the CLI starts it as a stdio MCP server.
It holds no tools of its own. Every ``tools/list`` and ``tools/call`` request is
forwarded over a Unix socket to the agent in the harness process, which answers
the list from the task's tool specs and runs each call through the bridge's
``call_tool``. Controls, ``blocked_by``, block messages, the step and time
limits and the trace therefore work exactly as they do for any outside agent.

The file imports only the standard library, so the CLI can start it with the
harness's interpreter and a file path, whatever that interpreter's import path
holds. It speaks the MCP stdio transport: one JSON-RPC 2.0 message per line on
stdin and stdout (https://modelcontextprotocol.io/specification/2025-06-18/basic/transports).
Tool calls are answered on their own threads, so calls the CLI makes in parallel
reach the harness together and the bridge takes them in arrival order.

Usage (by the CLI, never by hand)::

    python claude_code_mcp.py <relay socket path>
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
from typing import Any

#: The protocol version answered when the client names none.
PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "trace-harness", "version": "1"}
#: How long one relay exchange may take. A tool call waits for the harness
#: runner to execute its step, which the run's own time limit bounds.
RELAY_TIMEOUT_SECONDS = 3600.0

#: How long a call still in flight when stdin closes may take to answer.
DRAIN_SECONDS = 5.0

_write_lock = threading.Lock()
_calls: list[threading.Thread] = []


def relay(socket_path: str, request: dict[str, Any]) -> dict[str, Any]:
    """Send one request to the agent in the harness process and return its reply."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(RELAY_TIMEOUT_SECONDS)
        connection.connect(socket_path)
        connection.sendall(json.dumps(request).encode("utf-8") + b"\n")
        received = b""
        while not received.endswith(b"\n"):
            chunk = connection.recv(65536)
            if not chunk:
                break
            received += chunk
    reply = json.loads(received.decode("utf-8"))
    if not isinstance(reply, dict):
        raise ValueError("the harness relay answered with something other than an object")
    return reply


def send(message: dict[str, Any]) -> None:
    line = json.dumps(message, ensure_ascii=False) + "\n"
    with _write_lock:
        sys.stdout.write(line)
        sys.stdout.flush()


def answer(message_id: Any, result: dict[str, Any]) -> None:
    send({"jsonrpc": "2.0", "id": message_id, "result": result})


def fail(message_id: Any, code: int, text: str) -> None:
    send({"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": text}})


def call_tool(socket_path: str, message_id: Any, params: dict[str, Any]) -> None:
    """Run one tool call through the harness and answer it as an MCP tool result."""
    name = params.get("name")
    arguments = params.get("arguments")
    request = {"op": "call", "name": name, "arguments": arguments if arguments else {}}
    try:
        reply = relay(socket_path, request)
        text, is_error = str(reply["text"]), bool(reply["is_error"])
    except (OSError, ValueError, KeyError) as exc:
        text, is_error = f"the harness did not answer this tool call: {exc}", True
    answer(message_id, {"content": [{"type": "text", "text": text}], "isError": is_error})


def handle(socket_path: str, message: dict[str, Any]) -> None:
    method = message.get("method")
    message_id = message.get("id")
    if message_id is None:
        return  # a notification, such as notifications/initialized
    params = message.get("params") or {}
    if method == "initialize":
        requested = params.get("protocolVersion")
        answer(
            message_id,
            {
                "protocolVersion": requested if isinstance(requested, str) else PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
            },
        )
    elif method == "ping":
        answer(message_id, {})
    elif method == "tools/list":
        try:
            answer(message_id, {"tools": relay(socket_path, {"op": "list"})["tools"]})
        except (OSError, ValueError, KeyError) as exc:
            fail(message_id, -32603, f"the harness did not list its tools: {exc}")
    elif method == "tools/call":
        thread = threading.Thread(
            target=call_tool, args=(socket_path, message_id, params), daemon=True
        )
        _calls.append(thread)
        thread.start()
    else:
        fail(message_id, -32601, f"method not found: {method}")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: claude_code_mcp.py <relay socket path>", file=sys.stderr)
        return 2
    socket_path = argv[1]
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            fail(None, -32700, "parse error")
            continue
        if isinstance(message, dict):
            handle(socket_path, message)
    # The client closed its end. Answers still in flight get a moment to land.
    deadline = time.monotonic() + DRAIN_SECONDS
    for thread in _calls:
        thread.join(max(0.0, deadline - time.monotonic()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
