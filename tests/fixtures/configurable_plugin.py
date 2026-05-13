"""Test fixture: a plugin with a config schema we can poke at."""

from __future__ import annotations

from pilothouse.plugins import ConfigField, NotifierPlugin, PluginMeta
from pilothouse.events import RunEvent


class ConfigurablePlugin(NotifierPlugin):
    name = "configurable"

    def __init__(self) -> None:
        self.received_config: dict | None = None
        self.dispatched: list[str] = []

    def meta(self) -> PluginMeta:
        return PluginMeta(
            name=self.name,
            version="0.1.0",
            description="Test fixture: requires `webhook_url`, optional `prefix`.",
            kinds=set(self._inferred_kinds()),
        )

    def config_schema(self) -> list[ConfigField]:
        return [
            ConfigField(
                name="webhook_url",
                description="Where to send.",
                required=True,
                secret=True,
                env_fallback="CONFIGURABLE_WEBHOOK_URL",
            ),
            ConfigField(
                name="prefix",
                description="Optional message prefix.",
                required=False,
                default="[bot]",
            ),
        ]

    async def configure(self, config: dict) -> None:
        self.received_config = dict(config)

    def matches(self, event: RunEvent) -> bool:
        return event.kind == "approval_requested"

    async def dispatch(self, event: RunEvent) -> None:
        from tests.fixtures.sample_plugin_sink import EVENTS

        EVENTS.append(f"{event.run_id}:{(self.received_config or {}).get('prefix', '')}")
