"""Public testing helpers for plugin authors.

Stable, importable surface for unit-testing a plugin without spinning
up the full platform. Two layers:

  * **Sync helpers** for connector / template tests that don't need
    the event bus or a DB:
        - `mock_tool_context(...)`  — build a ToolContext for a handler
        - `make_event(...)`         — construct a RunEvent
        - `capture_events()`        — context manager that records
          everything published to the global bus

  * **Async fixture** for integration tests that need a fresh manager:
        - `temp_plugin_manager(*plugins)` — async context manager
          that registers the supplied plugin instances into a brand-
          new manager + DB session, yielding the manager.

These are deliberately tiny — the goal is "I can write a meaningful
unit test for my plugin in 5 lines" — and they intentionally don't
expose Pilothouse internals beyond what's documented here.
"""

from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from typing import AsyncIterator, Iterator

from .connectors.base import ToolContext
from .events import RunEvent, get_bus
from .plugins.base import Plugin
from .plugins.manager import PluginManager


# --- sync helpers --------------------------------------------------------


def mock_tool_context(
    *,
    dry_run: bool = True,
    run_id: str = "test-run",
    agent_id: str = "test-agent",
    params: dict | None = None,
) -> ToolContext:
    """Build a ToolContext suitable for invoking a connector tool handler.

    Defaults to dry_run=True so destructive tools short-circuit the
    same way they would in real runs. Override when you specifically
    want to test the live path.
    """

    async def _emit(_name: str, _data: dict) -> None:
        return None

    return ToolContext(
        run_id=run_id,
        agent_id=agent_id,
        dry_run=dry_run,
        params=dict(params or {}),
        emit=_emit,
    )


def make_event(
    kind: str,
    *,
    run_id: str = "test-run",
    data: dict | None = None,
) -> RunEvent:
    """Construct a `RunEvent` for feeding into a NotifierPlugin."""
    return RunEvent(run_id=run_id, kind=kind, data=dict(data or {}))


@contextmanager
def capture_events(topic: str = "*") -> Iterator[list[RunEvent]]:
    """Capture every event published to the bus during the `with` block.

    Usage:

        with capture_events() as events:
            get_bus().publish(make_event("tool_call", data={"tool": "x"}))
        assert events[0].kind == "tool_call"

    The returned list is appended to in real time; iterate after the
    `with` block exits to inspect the final tally.
    """
    bus = get_bus()
    captured: list[RunEvent] = []
    q = bus.subscribe_queue(topic)
    try:
        yield captured
        # Drain anything still pending. We don't block — drain only what
        # the bus has already delivered to our queue.
        while True:
            try:
                ev = q.get_nowait()
            except Exception:
                break
            captured.append(ev)
    finally:
        bus.unsubscribe(topic, q)


# --- async fixture for integration tests -------------------------------


@asynccontextmanager
async def temp_plugin_manager(*plugins: Plugin) -> AsyncIterator[PluginManager]:
    """Yield a fresh `PluginManager` with only the supplied plugins active.

    The supplied plugins are registered directly (no discovery), so
    plugin authors can construct their plugin instance, configure it
    however they want, and assert on registry side effects without
    touching the filesystem or env vars.

    Usage:

        async def test_my_plugin():
            plugin = MyPlugin()
            async with temp_plugin_manager(plugin) as mgr:
                # plugin.on_load + register has happened
                ...

    This is the same code path the production manager uses for
    activation, so coverage is real.
    """
    mgr = PluginManager()
    # Bypass discovery: synthesise a Discovered for each plugin and
    # walk the load_all flow. We can't easily reuse load_all without
    # discovery, so we replicate the small subset here.
    from .db import session
    from .models import PluginState
    from .plugins.manager import _Live
    from sqlalchemy import select

    async with session() as s:
        for plugin in plugins:
            meta = plugin.meta()
            row = (
                await s.execute(select(PluginState).where(PluginState.name == meta.name))
            ).scalar_one_or_none()
            if row is None:
                row = PluginState(
                    name=meta.name,
                    enabled=True,
                    version=meta.version,
                    description=meta.description,
                    source="test",
                    kinds_json=[k.value for k in meta.kinds],
                )
                s.add(row)
            else:
                row.enabled = True
                row.misconfig_reason = ""
            await s.flush()
            live = _Live(plugin=plugin, source="test", enabled=True)
            mgr._plugins[meta.name] = live
            resolved, misconfig = await mgr._resolve_config(s, plugin)
            live.misconfig_reason = misconfig
            row.misconfig_reason = misconfig
            if not misconfig:
                await mgr._call_configure(plugin, resolved)
                await mgr._activate(live)

    try:
        yield mgr
    finally:
        await mgr.shutdown()


__all__ = [
    "mock_tool_context",
    "make_event",
    "capture_events",
    "temp_plugin_manager",
]
