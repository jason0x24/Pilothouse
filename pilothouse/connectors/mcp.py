"""MCP (Model Context Protocol) adapter — stdio + HTTP transports.

Lets you register an MCP server as a Pilothouse connector. Two
transports are supported, picked by the `transport` field on the spec:

  * stdio:  Spawn a subprocess and speak JSON-RPC 2.0 over its stdin/
            stdout pipes (the original MCP transport, used by everything
            launched via `uvx`/`npx`).
  * http:   POST JSON-RPC requests to a single HTTP endpoint. The
            current MCP HTTP profile says responses come back inline on
            the POST response (or via SSE on the same URL for streaming
            servers). We implement the simpler request/response variant,
            which is what the majority of hosted MCP servers use today.

Both transports surface tools the same way: namespaced as
`<connector>_<tool>` in the global registry, with destructive flagging
honoured via `inputSchema.x-destructive` or the spec's
`destructive_tools` set.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
from dataclasses import dataclass, field
from typing import Any

import httpx

from .base import Connector, Tool, ToolContext, ToolResult, registry

log = logging.getLogger(__name__)


@dataclass
class McpServerSpec:
    name: str  # connector name
    transport: str = "stdio"  # "stdio" | "http"
    # stdio transport
    command: list[str] = field(default_factory=list)  # argv (e.g. ["uvx", "mcp-server-time"])
    env: dict[str, str] = field(default_factory=dict)
    # http transport
    url: str = ""  # e.g. https://mcp.example.com/rpc
    headers: dict[str, str] = field(default_factory=dict)  # auth etc.
    # shared
    destructive_tools: set[str] = field(default_factory=set)  # tool names to flag destructive
    description: str = ""


class _McpStdioClient:
    """Minimal JSON-RPC over stdio client for one MCP server process.

    Single-flight: every call awaits a response with a matching `id`.
    Concurrent callers serialise on `_lock` to keep the framing simple.
    """

    def __init__(self, spec: McpServerSpec) -> None:
        self.spec = spec
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._next_id = 1
        self._initialized = False
        self._tools: list[dict] = []

    async def ensure_started(self) -> None:
        if self._proc and self._proc.returncode is None:
            return
        argv = self.spec.command
        self._proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**self.spec.env} if self.spec.env else None,
        )
        await self._initialize()

    async def _initialize(self) -> None:
        if self._initialized:
            return
        # MCP initialize handshake. We advertise no client capabilities
        # — Pilothouse only consumes tools.
        await self._call(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "pilothouse", "version": "0.1.0"},
            },
        )
        # Spec requires sending an `initialized` notification after.
        await self._notify("notifications/initialized", {})
        listed = await self._call("tools/list", {})
        self._tools = list(listed.get("tools") or [])
        self._initialized = True

    async def list_tools(self) -> list[dict]:
        await self.ensure_started()
        return list(self._tools)

    async def call_tool(self, name: str, arguments: dict) -> dict:
        await self.ensure_started()
        return await self._call("tools/call", {"name": name, "arguments": arguments})

    async def stop(self) -> None:
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._proc.kill()

    async def _call(self, method: str, params: dict, timeout: float = 30.0) -> dict:
        assert self._proc and self._proc.stdin and self._proc.stdout, "process not started"
        async with self._lock:
            self._next_id += 1
            req_id = self._next_id
            envelope = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
                "params": params,
            }
            payload = (json.dumps(envelope) + "\n").encode("utf-8")
            self._proc.stdin.write(payload)
            await self._proc.stdin.drain()

            # Read until we see a response with matching id. Notifications
            # (no id) are skipped — we don't process server-initiated
            # work in this minimal client.
            deadline = asyncio.get_event_loop().time() + timeout
            while True:
                remaining = max(0.0, deadline - asyncio.get_event_loop().time())
                if remaining == 0:
                    raise TimeoutError(f"MCP call timed out: {method}")
                line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=remaining)
                if not line:
                    raise RuntimeError(
                        f"MCP server '{self.spec.name}' closed its stdout unexpectedly"
                    )
                try:
                    msg = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    continue
                if msg.get("id") != req_id:
                    continue
                if "error" in msg:
                    err = msg["error"]
                    raise RuntimeError(
                        f"MCP error {err.get('code')}: {err.get('message')}"
                    )
                return msg.get("result", {})

    async def _notify(self, method: str, params: dict) -> None:
        assert self._proc and self._proc.stdin, "process not started"
        envelope = {"jsonrpc": "2.0", "method": method, "params": params}
        payload = (json.dumps(envelope) + "\n").encode("utf-8")
        self._proc.stdin.write(payload)
        await self._proc.stdin.drain()


class _McpHttpClient:
    """JSON-RPC over HTTP. One POST per request; `id` echoed back in the
    response. We keep a single httpx.AsyncClient pool for the connector
    lifetime so connection reuse is cheap.

    For streaming MCP servers (SSE) the response would arrive as
    `text/event-stream` chunks; we don't implement that yet but the
    seam is clearly marked — `_call` would need to switch on the
    response Content-Type and consume an SSE stream when present.
    """

    def __init__(self, spec: McpServerSpec) -> None:
        if not spec.url:
            raise ValueError(f"http MCP server '{spec.name}' missing url")
        self.spec = spec
        self._client = httpx.AsyncClient(
            base_url=spec.url,
            headers={"content-type": "application/json", **spec.headers},
            timeout=30.0,
        )
        self._next_id = 1
        self._tools: list[dict] = []
        self._initialized = False
        self._lock = asyncio.Lock()

    async def ensure_started(self) -> None:
        if self._initialized:
            return
        await self._call(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "pilothouse", "version": "0.1.0"},
            },
        )
        # `initialized` notification — no id, no response expected.
        try:
            await self._client.post(
                "", json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
            )
        except Exception:
            pass
        listed = await self._call("tools/list", {})
        self._tools = list(listed.get("tools") or [])
        self._initialized = True

    async def list_tools(self) -> list[dict]:
        await self.ensure_started()
        return list(self._tools)

    async def call_tool(self, name: str, arguments: dict) -> dict:
        await self.ensure_started()
        return await self._call("tools/call", {"name": name, "arguments": arguments})

    async def stop(self) -> None:
        try:
            await self._client.aclose()
        except Exception:
            pass

    async def _call(self, method: str, params: dict) -> dict:
        async with self._lock:
            self._next_id += 1
            req_id = self._next_id
            envelope = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
            r = await self._client.post("", json=envelope)
            if r.status_code >= 400:
                raise RuntimeError(
                    f"MCP HTTP {r.status_code}: {r.text[:200]}"
                )
            try:
                msg = r.json()
            except Exception as exc:
                raise RuntimeError(f"MCP HTTP non-JSON response: {exc}") from exc
            if "error" in msg:
                err = msg["error"]
                raise RuntimeError(f"MCP error {err.get('code')}: {err.get('message')}")
            return msg.get("result", {})


class McpConnector(Connector):
    """A Pilothouse connector backed by a remote MCP server.

    Tools are populated lazily on first registration via `await sync()`.
    The connector keeps the underlying transport (subprocess for stdio,
    httpx pool for http) for the lifetime of the process.
    """

    def __init__(self, spec: McpServerSpec) -> None:
        super().__init__()
        self.name = spec.name
        self.spec = spec
        if spec.transport == "http":
            self._client: _McpStdioClient | _McpHttpClient = _McpHttpClient(spec)
        else:
            if not spec.command:
                raise ValueError(f"stdio MCP server '{spec.name}' missing command")
            self._client = _McpStdioClient(spec)

    @property
    def live(self) -> bool:
        return True  # MCP servers are always "live" — no mock mode

    async def sync(self) -> None:
        """Populate `self._tools` from the upstream MCP server."""
        upstream = await self._client.list_tools()
        self._tools = [self._make_tool(t) for t in upstream]

    async def stop(self) -> None:
        await self._client.stop()

    def _make_tool(self, mcp_tool: dict) -> Tool:
        tool_name = mcp_tool.get("name", "?")
        # Namespace under the connector name to avoid collisions across
        # multiple MCP servers (and with built-ins).
        local_name = f"{self.name}_{tool_name}"
        description = mcp_tool.get("description", "")
        schema = mcp_tool.get("inputSchema") or {"type": "object", "properties": {}}
        is_destructive = (
            schema.get("x-destructive") is True
            or tool_name in self.spec.destructive_tools
        )

        async def handler(ctx: ToolContext, params: dict) -> ToolResult:
            try:
                raw = await self._client.call_tool(tool_name, params)
            except Exception as exc:
                return ToolResult(content={"error": str(exc)}, is_error=True)
            # MCP tools/call returns {"content": [{"type":"text","text":"..."}], "isError": bool}
            text_parts: list[str] = []
            for block in raw.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
            if text_parts:
                return ToolResult(content="\n".join(text_parts), is_error=bool(raw.get("isError")))
            return ToolResult(content=raw, is_error=bool(raw.get("isError")))

        return Tool(
            name=local_name,
            description=description or f"MCP tool {tool_name} from {self.name}",
            input_schema=schema,
            handler=handler,
            is_destructive=is_destructive,
            connector=self.name,
        )


# --- public API ---------------------------------------------------------


_registered: dict[str, McpConnector] = {}


async def register_mcp_server(spec: McpServerSpec) -> McpConnector:
    """Spawn the server, sync tools, and add to the global registry.

    Idempotent: re-registering by name replaces the existing entry and
    stops the previous subprocess.
    """
    existing = _registered.pop(spec.name, None)
    if existing is not None:
        try:
            await existing.stop()
        except Exception:
            pass

    conn = McpConnector(spec)
    await conn.sync()
    registry.register(conn)
    _registered[spec.name] = conn
    return conn


async def unregister_mcp_server(name: str) -> bool:
    conn = _registered.pop(name, None)
    if conn is None:
        return False
    try:
        await conn.stop()
    finally:
        registry.connectors.pop(name, None)
    return True


def list_registered() -> list[str]:
    return list(_registered.keys())


def parse_command(s: str) -> list[str]:
    """Parse a shell-quoted command line into argv."""
    return shlex.split(s)
