"""Sample plugin used by the directory-discovery test.

Mixes two kinds (Connector + Notifier) to verify the manager handles
multi-kind plugins correctly.
"""

from __future__ import annotations

from pilothouse.connectors.base import Connector, Tool, ToolContext, ToolResult
from pilothouse.events import RunEvent
from pilothouse.plugins import ConnectorPlugin, NotifierPlugin, PluginMeta


class _EchoConnector(Connector):
    name = "echo"

    def __init__(self) -> None:
        super().__init__()

        async def _handler(ctx: ToolContext, params: dict) -> ToolResult:
            return ToolResult(content={"echoed": params.get("text", "")})

        self._add(
            "echo_say",
            "Echo back the text param.",
            {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            _handler,
        )


class SamplePlugin(ConnectorPlugin, NotifierPlugin):
    """Records approval_requested events into a *shared* sink module so
    the test can observe them regardless of which import path the
    plugin came from (directory discovery imports this file under a
    fresh module name, distinct from the test's own import)."""

    name = "sample"

    def meta(self) -> PluginMeta:
        return PluginMeta(
            name=self.name,
            version="0.1.0",
            description="Test fixture — echo connector + notifier sink.",
            kinds=set(self._inferred_kinds()),
        )

    def connectors(self) -> list:
        return [_EchoConnector()]

    def matches(self, event: RunEvent) -> bool:
        return event.kind == "approval_requested"

    async def dispatch(self, event: RunEvent) -> None:
        # Lazy import via the canonical name so both copies of this
        # module write to the same `EVENTS` list.
        from tests.fixtures.sample_plugin_sink import EVENTS

        EVENTS.append(event.run_id)
