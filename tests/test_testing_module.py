"""Tests for the public `pilothouse.testing` helpers.

The whole point of `pilothouse.testing` is that plugin authors should
never need to look inside Pilothouse to write meaningful unit tests.
These tests verify the documented surface works as advertised.
"""

from __future__ import annotations

import pytest

from pilothouse.events import RunEvent, get_bus, reset_bus
from pilothouse.plugins import ConfigField, NotifierPlugin, PluginMeta
from pilothouse.plugins.manager import reset_manager
from pilothouse.testing import (
    capture_events,
    make_event,
    mock_tool_context,
    temp_plugin_manager,
)


@pytest.fixture(autouse=True)
def _reset() -> None:
    reset_bus()
    reset_manager()
    yield
    reset_bus()
    reset_manager()


# --- mock_tool_context ---------------------------------------------------


def test_mock_tool_context_defaults_to_dry_run() -> None:
    ctx = mock_tool_context()
    assert ctx.dry_run is True


def test_mock_tool_context_overrides() -> None:
    ctx = mock_tool_context(
        dry_run=False, run_id="r-7", agent_id="a-2", params={"k": "v"}
    )
    assert ctx.dry_run is False
    assert ctx.run_id == "r-7"
    assert ctx.agent_id == "a-2"
    assert ctx.params == {"k": "v"}


async def test_mock_tool_context_emit_is_safe_no_op() -> None:
    ctx = mock_tool_context()
    # emit is async — calling it shouldn't raise.
    await ctx.emit("x", {"y": 1})


# --- make_event ---------------------------------------------------------


def test_make_event_sets_kind_and_data() -> None:
    ev = make_event("approval_requested", run_id="r-1", data={"approval_id": "a"})
    assert ev.kind == "approval_requested"
    assert ev.run_id == "r-1"
    assert ev.data == {"approval_id": "a"}


# --- capture_events -----------------------------------------------------


def test_capture_events_records_publishes() -> None:
    bus = get_bus()
    with capture_events() as events:
        bus.publish(RunEvent(run_id="r1", kind="tool_call", data={"tool": "x"}))
        bus.publish(RunEvent(run_id="r1", kind="tool_result", data={"tool": "x"}))
    kinds = [e.kind for e in events]
    assert kinds == ["tool_call", "tool_result"]


def test_capture_events_topic_filter() -> None:
    bus = get_bus()
    with capture_events(topic="r-only") as events:
        bus.publish(RunEvent(run_id="r-only", kind="x", data={}))
        bus.publish(RunEvent(run_id="other", kind="x", data={}))
    assert len(events) == 1
    assert events[0].run_id == "r-only"


# --- temp_plugin_manager ------------------------------------------------


class _TestPlugin(NotifierPlugin):
    name = "test_only"

    def __init__(self) -> None:
        self._target = ""

    def meta(self) -> PluginMeta:
        return PluginMeta(
            name=self.name,
            version="0.1.0",
            description="Sample plugin for the testing-module test.",
            kinds=set(self._inferred_kinds()),
        )

    def config_schema(self) -> list[ConfigField]:
        return [ConfigField(name="target", required=True)]

    async def configure(self, config: dict) -> None:
        self._target = config.get("target", "")

    def matches(self, event: RunEvent) -> bool:
        return event.kind == "approval_requested"

    async def dispatch(self, event: RunEvent) -> None:
        return None


async def test_temp_plugin_manager_marks_misconfigured_when_required_missing() -> None:
    async with temp_plugin_manager(_TestPlugin()) as mgr:
        rows = {p["name"]: p for p in mgr.list_plugins()}
        assert "test_only" in rows
        assert "missing required config" in rows["test_only"]["misconfig_reason"]


async def test_temp_plugin_manager_satisfies_via_set_config() -> None:
    p = _TestPlugin()
    async with temp_plugin_manager(p) as mgr:
        # Initially flagged misconfigured.
        await mgr.set_config("test_only", "target", "https://x")
        assert mgr.doctor() == []
        # configure() actually got called with the value.
        assert p._target == "https://x"
