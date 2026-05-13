"""Example TriggerPlugin — poll a URL and trigger an agent on changes.

Demonstrates the under-documented trigger surface. The plugin polls a
configured URL on a configured interval; when the response body's
SHA-256 changes from the last poll, it fires a configured agent with
the new payload as `trigger_payload`.

Use cases this generalises to:
  * "Re-trigger SLO triage when the alert state file in S3 changes"
  * "Sync external feature-flag state into Pilothouse runs"
  * "Replace a 100-line cron + diff + curl script with one declarative
     plugin"

Install:
  pilothouse plugins config set poll_url_trigger url https://status.example/api.json
  pilothouse plugins config set poll_url_trigger agent_id 84e6ed9d-…
  pilothouse plugins enable poll_url_trigger        # if previously disabled
"""

from __future__ import annotations

import asyncio
import hashlib
import logging

import httpx

from pilothouse.plugins import ConfigField, PluginMeta, TriggerPlugin

log = logging.getLogger(__name__)


class PollUrlTriggerPlugin(TriggerPlugin):
    name = "poll_url_trigger"

    def __init__(self) -> None:
        self._url = ""
        self._agent_id = ""
        self._interval = 60
        self._last_hash = ""
        self._task: asyncio.Task | None = None

    def meta(self) -> PluginMeta:
        return PluginMeta(
            name=self.name,
            version="0.1.0",
            description="Poll a URL on an interval and trigger an agent when the body changes.",
            kinds=set(self._inferred_kinds()),
        )

    def config_schema(self) -> list[ConfigField]:
        return [
            ConfigField(
                name="url",
                description="Resource to poll (GET).",
                required=True,
            ),
            ConfigField(
                name="agent_id",
                description="Pilothouse agent id to trigger when the response changes.",
                required=True,
            ),
            ConfigField(
                name="interval_seconds",
                description="How often to poll. Lower bound = 5s.",
                required=False,
                default="60",
            ),
        ]

    async def configure(self, config: dict) -> None:
        self._url = config.get("url", "").strip()
        self._agent_id = config.get("agent_id", "").strip()
        try:
            self._interval = max(5, int(config.get("interval_seconds", "60") or "60"))
        except ValueError:
            self._interval = 60

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        if not self._url or not self._agent_id:
            return  # Manager already flagged misconfig; just don't start the loop.
        self._task = asyncio.create_task(self._run(), name=f"trigger-{self.name}")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        self._task = None

    async def _run(self) -> None:
        from pilothouse.orchestration import execute_agent

        try:
            while True:
                try:
                    async with httpx.AsyncClient(timeout=10) as client:
                        r = await client.get(self._url)
                    body = r.content
                    digest = hashlib.sha256(body).hexdigest()
                    if digest != self._last_hash:
                        # Only trigger after the FIRST observation has set
                        # a baseline — that way fresh installs don't
                        # immediately fire on every URL.
                        if self._last_hash:
                            try:
                                payload = r.json()
                            except Exception:
                                payload = {"raw": body[:2000].decode("utf-8", errors="replace")}
                            await execute_agent(
                                agent_id=self._agent_id,
                                trigger=f"plugin:{self.name}",
                                trigger_payload=payload,
                            )
                        self._last_hash = digest
                except Exception:
                    log.exception("poll_url_trigger poll failed")
                await asyncio.sleep(self._interval)
        except asyncio.CancelledError:
            return
