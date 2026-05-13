#!/usr/bin/env python3
"""Tiny MCP server used by tests.

Speaks just enough of MCP JSON-RPC over stdio to:
  - respond to `initialize`
  - serve `tools/list` with one read tool + one destructive tool
  - respond to `tools/call` with a deterministic echo

Behaviour is the bare minimum for the Pilothouse MCP adapter tests.
"""

import json
import sys


TOOLS = [
    {
        "name": "echo",
        "description": "Return the input args as text.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "delete_thing",
        "description": "Pretend to delete something. Marked destructive via x-destructive.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
            "x-destructive": True,
        },
    },
]


def reply(req_id, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": req_id}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result or {}
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main() -> None:
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        req_id = msg.get("id")

        if method == "initialize":
            reply(
                req_id,
                {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "mock-mcp", "version": "0.1.0"},
                },
            )
        elif method == "notifications/initialized":
            # notifications have no id, no response
            continue
        elif method == "tools/list":
            reply(req_id, {"tools": TOOLS})
        elif method == "tools/call":
            params = msg.get("params") or {}
            tool_name = params.get("name")
            args = params.get("arguments") or {}
            if tool_name == "echo":
                reply(req_id, {"content": [{"type": "text", "text": f"echo:{args.get('text', '')}"}]})
            elif tool_name == "delete_thing":
                reply(
                    req_id,
                    {
                        "content": [{"type": "text", "text": f"deleted {args.get('id')}"}],
                        "isError": False,
                    },
                )
            else:
                reply(req_id, error={"code": -32601, "message": f"unknown tool: {tool_name}"})
        else:
            if req_id is not None:
                reply(req_id, error={"code": -32601, "message": f"unknown method: {method}"})


if __name__ == "__main__":
    main()
