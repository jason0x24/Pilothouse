"""Example HookPlugin — forward run lifecycle to a SIEM / data lake.

Every started Run produces a `before_run` event; every terminal Run
produces an `after_run` event with the final status + summary. This
plugin POSTs both to a configurable URL in a SIEM-friendly envelope.

Why this exists as an example
-----------------------------

`HookPlugin` is the under-documented kind — most templates / connectors
have an obvious shape, but hooks are subtle: they run in the
orchestration path so they must be fast and never raise. This file
shows the conservative production pattern:

  * Fan out via a bounded `asyncio.Queue` so the orchestration path
    never blocks on slow HTTP.
  * Background worker drains the queue at its own pace.
  * Backpressure is *drop oldest* — never block the agent.
  * Errors are logged but never bubble.

Drop-in install
---------------

  pilothouse plugins config set siem_audit_forwarder \
      url https://splunk.example/services/collector/raw
  pilothouse plugins config set siem_audit_forwarder \
      auth_header 'Splunk <hec-token>'

That's it. After `plugins reload`, every run start/end gets forwarded.

JSON envelope it sends
----------------------

  {
    "source": "pilothouse",
    "event": "run_started" | "run_finished",
    "run_id": "...", "agent_id": "...", "tenant_id": "...",
    "status": "succeeded"|"failed"|"cancelled",   # finished only
    "summary": "...",                             # finished only
    "ts": "2026-05-13T10:00:00Z"
  }
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

import httpx

from pilothouse.plugins import ConfigField, HookPlugin, PluginMeta

log = logging.getLogger(__name__)

# How many pending events to buffer before we start dropping the
# oldest. Tuned for "very chatty during incidents" — 1000 events
# covers ~30 minutes of sustained 0.5 Hz traffic with headroom.
_QUEUE_MAX = 1000


class SiemAuditForwarderPlugin(HookPlugin):
    name = "siem_audit_forwarder"

    def __init__(self) -> None:
        self._url = ""
        self._auth_header = ""
        self._timeout_seconds = 5.0
        self._queue: asyncio.Queue[dict] | None = None
        self._worker_task: asyncio.Task | None = None

    def meta(self) -> PluginMeta:
        return PluginMeta(
            name=self.name,
            version="0.1.0",
            description="Forward run start/finish lifecycle events to a SIEM endpoint.",
            kinds=set(self._inferred_kinds()),
        )

    def config_schema(self) -> list[ConfigField]:
        return [
            ConfigField(
                name="url",
                description="HTTP endpoint that receives the JSON envelope per event.",
                required=True,
                secret=False,
                env_fallback="SIEM_FORWARDER_URL",
            ),
            ConfigField(
                name="auth_header",
                description="Value for the `Authorization` header. Leave empty for public collectors.",
                required=False,
                secret=True,
                env_fallback="SIEM_FORWARDER_AUTH",
            ),
            ConfigField(
                name="timeout_seconds",
                description="Per-request HTTP timeout in seconds.",
                required=False,
                default="5",
            ),
        ]

    async def configure(self, config: dict) -> None:
        self._url = config.get("url", "").strip()
        self._auth_header = config.get("auth_header", "").strip()
        try:
            self._timeout_seconds = float(config.get("timeout_seconds", "5") or "5")
        except ValueError:
            self._timeout_seconds = 5.0

    async def on_load(self) -> None:
        """Spin up the background flusher. Called by the manager exactly
        once when the plugin is activated."""
        if not self._url:
            return  # manager already flagged misconfig; nothing to do
        self._queue = asyncio.Queue(maxsize=_QUEUE_MAX)
        self._worker_task = asyncio.create_task(
            self._drain(), name=f"plugin-{self.name}-drain"
        )

    async def on_unload(self) -> None:
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except (asyncio.CancelledError, Exception):
                pass
            self._worker_task = None
        # The queue may still hold events; discard them on unload to
        # avoid them being sent against a disabled / mis-configured
        # endpoint after re-enable. If you want at-least-once delivery,
        # persist the queue to disk here instead.
        self._queue = None

    # --- the actual hooks ---------------------------------------------

    async def before_run(
        self, *, run_id: str, agent_id: str, tenant_id: str
    ) -> None:
        self._enqueue(
            {
                "event": "run_started",
                "run_id": run_id,
                "agent_id": agent_id,
                "tenant_id": tenant_id,
            }
        )

    async def after_run(
        self, *, run_id: str, agent_id: str, tenant_id: str, status: str, summary: str
    ) -> None:
        self._enqueue(
            {
                "event": "run_finished",
                "run_id": run_id,
                "agent_id": agent_id,
                "tenant_id": tenant_id,
                "status": status,
                # SIEM ingestion paths typically cap line length; we
                # truncate so a single multi-thousand-line summary
                # doesn't get dropped by a downstream length policy.
                "summary": (summary or "")[:2000],
            }
        )

    # --- internals -----------------------------------------------------

    def _enqueue(self, event: dict) -> None:
        """Non-blocking enqueue. If the queue is full, drop the oldest
        event — the orchestration path NEVER blocks on the SIEM."""
        if self._queue is None:
            return
        event.update(
            {
                "source": "pilothouse",
                "ts": datetime.now(timezone.utc).isoformat(),
            }
        )
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()  # drop oldest
            except Exception:
                pass
            try:
                self._queue.put_nowait(event)
            except Exception:
                pass

    async def _drain(self) -> None:
        """Forward events as they arrive. One in-flight POST at a time —
        keeps things simple; the queue is the buffer."""
        headers: dict[str, str] = {"content-type": "application/json"}
        if self._auth_header:
            headers["Authorization"] = self._auth_header
        try:
            while True:
                assert self._queue is not None
                event = await self._queue.get()
                try:
                    async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                        r = await client.post(
                            self._url, headers=headers, content=json.dumps(event)
                        )
                        if r.status_code >= 400:
                            log.warning(
                                "SIEM forwarder got HTTP %s: %s",
                                r.status_code,
                                r.text[:200],
                            )
                except Exception:
                    log.exception("SIEM forwarder POST failed (event dropped)")
        except asyncio.CancelledError:
            return
