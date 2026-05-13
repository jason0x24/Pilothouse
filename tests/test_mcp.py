"""MCP adapter tests via a hand-rolled stdio mock server."""

from __future__ import annotations

import sys
from pathlib import Path

from pilothouse.connectors.base import registry
from pilothouse.connectors.mcp import McpServerSpec, register_mcp_server, unregister_mcp_server


FIXTURE = Path(__file__).parent / "fixtures" / "mock_mcp_server.py"


async def test_register_mcp_server_exposes_tools_in_registry() -> None:
    spec = McpServerSpec(name="mock", command=[sys.executable, str(FIXTURE)])
    conn = await register_mcp_server(spec)
    try:
        assert "mock" in registry.connectors
        names = {t.name for t in conn.tools()}
        assert "mock_echo" in names
        assert "mock_delete_thing" in names
        # x-destructive is honoured.
        delete_tool = next(t for t in conn.tools() if t.name == "mock_delete_thing")
        assert delete_tool.is_destructive
        echo_tool = next(t for t in conn.tools() if t.name == "mock_echo")
        assert not echo_tool.is_destructive
    finally:
        await unregister_mcp_server("mock")


async def test_mcp_tool_call_returns_text_content() -> None:
    spec = McpServerSpec(name="mock", command=[sys.executable, str(FIXTURE)])
    conn = await register_mcp_server(spec)
    try:
        echo = next(t for t in conn.tools() if t.name == "mock_echo")
        from pilothouse.connectors.base import ToolContext

        async def _emit(_n, _d):
            return None

        ctx = ToolContext(run_id="r", agent_id="a", dry_run=True, params={}, emit=_emit)
        result = await echo.handler(ctx, {"text": "hello"})
        assert not result.is_error
        assert "echo:hello" in result.content
    finally:
        await unregister_mcp_server("mock")


async def test_destructive_mcp_tool_short_circuits_in_dry_run() -> None:
    """The runtime — not the connector — enforces dry-run for destructive
    MCP tools, exactly as for built-in connectors. We assert by running
    the same agent loop and checking the tool_result event payload."""
    from sqlalchemy import select

    from pilothouse.db import session
    from pilothouse.models import Agent, Event, EventKind, Run, RunStatus
    from pilothouse.orchestration.executor import execute_agent

    spec = McpServerSpec(name="mock", command=[sys.executable, str(FIXTURE)])
    await register_mcp_server(spec)
    try:
        # Build a one-off template inline by piggy-backing on the existing
        # DatadogAlertTriage and replacing its mock_plan via the registry.
        # Simpler: register a small ad-hoc template via the public registry.
        from pilothouse.templates.base import Template, TemplatePlan, registry as tpl_registry

        class _MockMcpDriver(Template):
            key = "test_mcp_driver"
            name = "Test MCP Driver"
            description = "internal"
            default_tools = ["mock"]

            def plan(self, *, trigger_payload, params):
                return TemplatePlan(
                    system_prompt="test",
                    user_message="test",
                    tool_names=self.default_tools,
                )

            def mock_plan(self, *, trigger_payload, params):
                return [
                    {"tool": "mock_delete_thing", "input": {"id": "abc"}},
                    {"final": "done"},
                ]

        tpl_registry.register(_MockMcpDriver())

        async with session() as s:
            a = Agent(name="mcp-driver", template="test_mcp_driver", params={}, dry_run=True)
            s.add(a)
            await s.flush()
            aid = a.id

        run_id = await execute_agent(agent_id=aid, trigger="manual", trigger_payload={})
        async with session() as s:
            run = (await s.execute(select(Run).where(Run.id == run_id))).scalar_one()
            events = (
                await s.execute(select(Event).where(Event.run_id == run_id))
            ).scalars().all()
        assert run.status == RunStatus.succeeded
        tool_results = [e for e in events if e.kind == EventKind.tool_result]
        assert tool_results
        # Destructive tool short-circuited by runtime in dry-run.
        assert "dry_run" in tool_results[0].data["preview"]
    finally:
        await unregister_mcp_server("mock")
