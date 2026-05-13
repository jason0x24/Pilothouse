"""Example Discord notifier plugin.

Drop-in example showing the full third-party plugin pattern:
  * directory-based discovery (no pip install needed)
  * declared `config_schema` so operators set the URL without touching env
    vars (env_fallback still works as a back-compat hatch)
  * `matches` + `dispatch` on the notifier surface

Install:
  1. Set `PILOTHOUSE_PLUGIN_DIR=/path/to/this/directory` (or copy this
     file into `./plugins/` next to where you run pilothouse).
  2. `pilothouse plugins reload`.
  3. Set the webhook URL — either way works:
        pilothouse plugins config set discord_notifier webhook_url https://discord.com/api/webhooks/...
        # OR
        export DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
  4. `pilothouse plugins list` now shows `discord_notifier` enabled and
     not misconfigured.

The plugin posts to Discord when an approval is requested or a run fails.
"""

from __future__ import annotations

import httpx

from pilothouse.events import RunEvent
from pilothouse.plugins import ConfigField, NotifierPlugin, PluginMeta


class DiscordNotifierPlugin(NotifierPlugin):
    name = "discord_notifier"

    def __init__(self) -> None:
        self._webhook_url = ""

    def meta(self) -> PluginMeta:
        return PluginMeta(
            name=self.name,
            version="0.2.0",
            description="Posts approval / failure events to a Discord channel webhook.",
            kinds=set(self._inferred_kinds()),
        )

    def config_schema(self) -> list[ConfigField]:
        return [
            ConfigField(
                name="webhook_url",
                description="Discord channel incoming webhook URL.",
                required=True,
                secret=True,
                env_fallback="DISCORD_WEBHOOK_URL",
            ),
        ]

    async def configure(self, config: dict) -> None:
        self._webhook_url = config.get("webhook_url", "").strip()

    def matches(self, event: RunEvent) -> bool:
        # Two event kinds qualify for Discord:
        #   * approval_requested → someone needs to decide
        #   * run_terminal (status in {failed, cancelled}) → ops awareness
        if event.kind == "approval_requested":
            return True
        if event.kind == "run_terminal":
            return event.data.get("status") in {"failed", "cancelled"}
        return False

    async def dispatch(self, event: RunEvent) -> None:
        if not self._webhook_url:
            # Manager won't activate us with a missing required config, so
            # this is just defence in depth.
            return

        if event.kind == "approval_requested":
            content = (
                f":pause_button: **Approval needed**\n"
                f"Run `{event.run_id[:8]}` wants to run "
                f"`{event.data.get('tool', '?')}`.\n"
                f"Approval id: `{event.data.get('approval_id', '?')[:8]}`."
            )
        else:
            status = event.data.get("status", "?")
            content = (
                f":x: **Run {status}** — run `{event.run_id[:8]}`.\n"
                f"```{event.data.get('summary_preview', '')[:500]}```"
            )

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(self._webhook_url, json={"content": content})
        except Exception:
            # Plugin errors must never crash the runtime. The manager
            # catches and logs, but defensive code here keeps the
            # surface small.
            pass
