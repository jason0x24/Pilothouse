"""MCP HTTP transport — wire the connector against a tiny FastAPI mock
that speaks just enough JSON-RPC for Pilothouse to discover + call tools.

We can't easily spin a real socket inside a pytest async test, so we
monkeypatch httpx.AsyncClient.post to route to an in-process handler.
"""

from __future__ import annotations

import json

import httpx
import pytest

from pilothouse.connectors.base import ToolContext, registry
from pilothouse.connectors.mcp import (
    McpServerSpec,
    register_mcp_server,
    unregister_mcp_server,
)


TOOLS = [
    {
        "name": "convert_currency",
        "description": "Convert an amount between currencies.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "amount": {"type": "number"},
                "from": {"type": "string"},
                "to": {"type": "string"},
            },
            "required": ["amount", "from", "to"],
        },
    },
    {
        "name": "wipe_account",
        "description": "Delete an account. Destructive.",
        "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "x-destructive": True},
    },
]


def _handle(envelope: dict) -> dict:
    """Pure-function MCP server — returns the response envelope."""
    method = envelope.get("method")
    req_id = envelope.get("id")
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}},
        }
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = envelope.get("params") or {}
        if params.get("name") == "convert_currency":
            args = params.get("arguments") or {}
            text = f"{args['amount']} {args['from']} = {args['amount'] * 1.1} {args['to']}"
            return {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": text}]}}
        if params.get("name") == "wipe_account":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": "deleted"}]}}
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "unknown"}}


@pytest.fixture(autouse=True)
def _patch_httpx(monkeypatch):
    async def fake_post(self, path, *, json=None, **kw):  # noqa: A002 - mirroring httpx
        body = json
        if isinstance(body, str):
            body = json.loads(body)
        if body is None:
            body = {}
        if "method" not in body:
            return httpx.Response(400, json={"detail": "no method"})
        if body["method"] == "notifications/initialized":
            # Notifications return 200 with no body in the real spec.
            return httpx.Response(200, json={})
        return httpx.Response(200, json=_handle(body))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)


async def test_http_mcp_register_lists_tools_with_destructive_flag() -> None:
    spec = McpServerSpec(
        name="finance",
        transport="http",
        url="https://mcp.example/rpc",
        headers={"Authorization": "Bearer test"},
    )
    conn = await register_mcp_server(spec)
    try:
        assert "finance" in registry.connectors
        names = {t.name for t in conn.tools()}
        assert "finance_convert_currency" in names
        assert "finance_wipe_account" in names
        wipe = next(t for t in conn.tools() if t.name == "finance_wipe_account")
        convert = next(t for t in conn.tools() if t.name == "finance_convert_currency")
        assert wipe.is_destructive
        assert not convert.is_destructive
    finally:
        await unregister_mcp_server("finance")


async def test_http_mcp_tool_call_returns_text() -> None:
    spec = McpServerSpec(
        name="finance",
        transport="http",
        url="https://mcp.example/rpc",
    )
    conn = await register_mcp_server(spec)
    try:
        tool = next(t for t in conn.tools() if t.name == "finance_convert_currency")

        async def _emit(_n, _d):
            return None

        ctx = ToolContext(run_id="r", agent_id="a", dry_run=True, params={}, emit=_emit)
        result = await tool.handler(ctx, {"amount": 100, "from": "USD", "to": "EUR"})
        assert "100" in result.content and "EUR" in result.content
    finally:
        await unregister_mcp_server("finance")


async def test_http_spec_without_url_fails_to_register() -> None:
    """The dataclass itself doesn't validate (the spec defaults are
    permissive), but the connector constructor refuses an empty URL —
    that's the boundary where bad config gets caught."""
    spec = McpServerSpec(name="bad", transport="http", url="")
    with pytest.raises(ValueError):
        await register_mcp_server(spec)
