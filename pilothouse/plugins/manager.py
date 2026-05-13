"""Plugin manager — discovery + lifecycle + persistence + registry wiring.

Single source of truth for "what plugins exist, what state are they in,
where do their contributions live." Built once per process, called
from the API server's lifespan and from the CLI.

Lifecycle:

  PluginManager.load_all() is called at startup. For each Discovered
  plugin it:
    1. Upserts a row in the `plugins` table (so the operator sees it).
    2. If the row says `enabled=true`, calls `_activate(plugin)` which
       registers the plugin's contributions with the right global
       registries (template_registry / connector_registry / event bus
       subscription / etc.) and invokes `on_load`.
    3. If `enabled=false`, the plugin is recorded but never activated.

  PluginManager.enable(name) / disable(name) flips the DB flag and then
  activates/deactivates the live plugin in-place — no restart needed.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import select

from .base import (
    ConfigField,
    ConnectorPlugin,
    HookPlugin,
    NotifierPlugin,
    Plugin,
    PluginKind,
    TemplatePlugin,
    TriggerPlugin,
)
from .discovery import Discovered, discover_all

log = logging.getLogger(__name__)


@dataclass
class _Live:
    plugin: Plugin
    source: str
    enabled: bool
    misconfig_reason: str = ""  # non-empty → required config missing → not activated
    notifier_queue: Optional[asyncio.Queue] = field(default=None)
    notifier_task: Optional[asyncio.Task] = field(default=None)
    triggered_started: bool = False


class PluginManager:
    """Singleton-ish (one per process via `get_manager()`).

    Not async-safe for concurrent enable/disable from multiple coroutines
    on the same plugin name — wrap calls in a lock at the API layer if
    you expose them concurrently. For MVP the CLI is single-threaded and
    the HTTP endpoints serialise via the asyncio event loop.
    """

    def __init__(self) -> None:
        self._plugins: dict[str, _Live] = {}

    # --- discovery + load -------------------------------------------------

    async def load_all(self) -> None:
        """Discover, persist, and activate all plugins that are enabled.

        Idempotent — safe to call multiple times. Re-discovers from
        scratch each call (cheap; in-tree imports are cached, entry
        points scan installed dists, directory scan re-imports).
        """
        from ..db import session
        from ..models import PluginState

        seen: set[str] = set()
        async with session() as s:
            for d in discover_all():
                if d.plugin.name in seen:
                    log.info("skipping duplicate plugin name=%s source=%s", d.plugin.name, d.source)
                    continue
                seen.add(d.plugin.name)
                meta = d.plugin.meta()

                row = (
                    await s.execute(select(PluginState).where(PluginState.name == meta.name))
                ).scalar_one_or_none()
                if row is None:
                    row = PluginState(
                        name=meta.name,
                        enabled=True,
                        version=meta.version,
                        description=meta.description,
                        source=d.source,
                        kinds_json=[k.value for k in meta.kinds],
                    )
                    s.add(row)
                else:
                    row.version = meta.version
                    row.description = meta.description
                    row.source = d.source
                    row.kinds_json = [k.value for k in meta.kinds]
                await s.flush()

                live = _Live(plugin=d.plugin, source=d.source, enabled=row.enabled)
                self._plugins[meta.name] = live
                if row.enabled:
                    # Resolve the plugin's config schema. Required-but-empty
                    # fields produce a misconfig reason; we still record
                    # the plugin row but skip activation.
                    resolved, misconfig = await self._resolve_config(s, d.plugin)
                    live.misconfig_reason = misconfig
                    row.misconfig_reason = misconfig
                    if misconfig:
                        try:
                            await self._deactivate(live)
                        except Exception:
                            log.exception("removing misconfigured plugin %s failed", meta.name)
                    else:
                        try:
                            await self._call_configure(d.plugin, resolved)
                            await self._activate(live)
                        except Exception:
                            log.exception("activating plugin %s failed", meta.name)
                else:
                    # Disabled in DB → make sure it's not lingering in the
                    # registry. The CLI's `_bootstrap()` back-compat shim
                    # registers every built-in eagerly; without an explicit
                    # deactivate here, that registration would survive.
                    row.misconfig_reason = ""
                    try:
                        await self._deactivate(live)
                    except Exception:
                        log.exception("ensuring plugin %s deactivated failed", meta.name)

    # --- enable / disable ------------------------------------------------

    async def enable(self, name: str) -> None:
        live = self._require(name)
        if live.enabled:
            return
        from ..db import session
        from ..models import PluginState

        async with session() as s:
            row = (
                await s.execute(select(PluginState).where(PluginState.name == name))
            ).scalar_one()
            row.enabled = True
        live.enabled = True
        await self._activate(live)

    async def disable(self, name: str) -> None:
        live = self._require(name)
        if not live.enabled:
            return
        from ..db import session
        from ..models import PluginState

        async with session() as s:
            row = (
                await s.execute(select(PluginState).where(PluginState.name == name))
            ).scalar_one()
            row.enabled = False
        live.enabled = False
        await self._deactivate(live)

    async def shutdown(self) -> None:
        """Tear down every active plugin — called from API lifespan finally."""
        for live in list(self._plugins.values()):
            if live.enabled:
                try:
                    await self._deactivate(live)
                except Exception:
                    log.exception("plugin %s teardown failed", live.plugin.name)

    # --- introspection ---------------------------------------------------

    def list_plugins(self) -> list[dict]:
        out = []
        for name, live in sorted(self._plugins.items()):
            meta = live.plugin.meta()
            schema = []
            try:
                schema = [
                    {
                        "name": f.name,
                        "description": f.description,
                        "required": f.required,
                        "secret": f.secret,
                        "default": f.default,
                        "env_fallback": f.env_fallback,
                    }
                    for f in live.plugin.config_schema()
                ]
            except Exception:
                log.exception("plugin %s config_schema raised", name)
            out.append(
                {
                    "name": name,
                    "version": meta.version,
                    "description": meta.description,
                    "kinds": sorted(k.value for k in meta.kinds),
                    "source": live.source,
                    "enabled": live.enabled,
                    "compat": meta.pilothouse_compat,
                    "misconfig_reason": live.misconfig_reason,
                    "config_schema": schema,
                }
            )
        return out

    def get_hooks(self) -> list[HookPlugin]:
        return [
            live.plugin for live in self._plugins.values()
            if live.enabled and not live.misconfig_reason and isinstance(live.plugin, HookPlugin)
        ]

    # --- config CRUD ----------------------------------------------------

    async def get_config(self, name: str, *, mask_secrets: bool = True) -> dict:
        """Return the resolved config for a plugin.

        Each schema field gets a dict: {value, source, secret, required}.
        `source` is "operator" / "env:<VAR>" / "default" / "" (unset).
        Secrets are masked unless explicitly disabled — useful for the
        UI and for ops audits.
        """
        live = self._require(name)
        from ..db import session

        async with session() as s:
            resolved, _ = await self._resolve_config(s, live.plugin, with_sources=True)
        out: dict[str, dict] = {}
        for f in live.plugin.config_schema():
            entry = resolved.get(f.name, {"value": "", "source": ""})
            value = entry["value"]
            if mask_secrets and f.secret and value:
                value = "***" + value[-4:] if len(value) > 4 else "***"
            out[f.name] = {
                "value": value,
                "source": entry["source"],
                "secret": f.secret,
                "required": f.required,
                "description": f.description,
            }
        return out

    async def set_config(self, name: str, key: str, value: str) -> None:
        """Set one config value for a plugin and re-activate.

        The set takes effect immediately: we deactivate, re-resolve the
        full schema, re-activate. Errors during re-activation surface
        as exceptions to the caller.
        """
        live = self._require(name)
        from ..db import session
        from ..models import PluginConfig

        # Validate the key is in the schema (typos shouldn't silently
        # store unused state).
        valid_keys = {f.name for f in live.plugin.config_schema()}
        if key not in valid_keys:
            raise KeyError(f"unknown config key {key!r} for plugin {name}")

        async with session() as s:
            row = (
                await s.execute(
                    select(PluginConfig).where(
                        PluginConfig.plugin_name == name, PluginConfig.key == key
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                row = PluginConfig(plugin_name=name, key=key, value=value)
                s.add(row)
            else:
                row.value = value

        # Re-resolve + reactivate in-place.
        if live.enabled:
            await self._deactivate(live)
            async with session() as s:
                resolved, misconfig = await self._resolve_config(s, live.plugin)
                from ..models import PluginState
                row = (
                    await s.execute(select(PluginState).where(PluginState.name == name))
                ).scalar_one()
                row.misconfig_reason = misconfig
            live.misconfig_reason = misconfig
            if not misconfig:
                await self._call_configure(live.plugin, resolved)
                await self._activate(live)

    async def unset_config(self, name: str, key: str) -> None:
        """Remove an operator-set value (env_fallback / default may still resolve)."""
        live = self._require(name)
        from ..db import session
        from ..models import PluginConfig

        async with session() as s:
            row = (
                await s.execute(
                    select(PluginConfig).where(
                        PluginConfig.plugin_name == name, PluginConfig.key == key
                    )
                )
            ).scalar_one_or_none()
            if row is not None:
                await s.delete(row)
        # Re-resolve as in set_config.
        if live.enabled:
            await self._deactivate(live)
            async with session() as s:
                resolved, misconfig = await self._resolve_config(s, live.plugin)
                from ..models import PluginState
                state_row = (
                    await s.execute(select(PluginState).where(PluginState.name == name))
                ).scalar_one()
                state_row.misconfig_reason = misconfig
            live.misconfig_reason = misconfig
            if not misconfig:
                await self._call_configure(live.plugin, resolved)
                await self._activate(live)

    def doctor(self) -> list[dict]:
        """List plugins that are enabled but cannot run due to bad config.

        Surfaced via `pilothouse plugins doctor` and the UI banner.
        """
        return [
            {
                "name": live.plugin.name,
                "reason": live.misconfig_reason,
            }
            for live in self._plugins.values()
            if live.enabled and live.misconfig_reason
        ]

    # --- internal --------------------------------------------------------

    def _require(self, name: str) -> _Live:
        if name not in self._plugins:
            raise KeyError(f"unknown plugin: {name}")
        return self._plugins[name]

    async def _activate(self, live: _Live) -> None:
        from ..connectors.base import registry as conn_registry
        from ..events import get_bus
        from ..templates.base import registry as tpl_registry

        plugin = live.plugin

        if isinstance(plugin, TemplatePlugin):
            for t in plugin.templates():
                tpl_registry.register(t)
        if isinstance(plugin, ConnectorPlugin):
            for c in plugin.connectors():
                conn_registry.register(c)
        if isinstance(plugin, NotifierPlugin):
            bus = get_bus()
            q = bus.subscribe_queue("*")
            live.notifier_queue = q
            live.notifier_task = asyncio.create_task(
                _drive_notifier(plugin, q), name=f"plugin-notify-{plugin.name}"
            )
        if isinstance(plugin, TriggerPlugin):
            try:
                await plugin.start()
                live.triggered_started = True
            except Exception:
                log.exception("trigger plugin %s failed to start", plugin.name)

        try:
            await plugin.on_load()
        except Exception:
            log.exception("plugin %s on_load raised", plugin.name)

    async def _deactivate(self, live: _Live) -> None:
        from ..connectors.base import registry as conn_registry
        from ..events import get_bus
        from ..templates.base import registry as tpl_registry

        plugin = live.plugin

        if isinstance(plugin, NotifierPlugin):
            if live.notifier_task is not None:
                live.notifier_task.cancel()
                live.notifier_task = None
            if live.notifier_queue is not None:
                get_bus().unsubscribe("*", live.notifier_queue)
                live.notifier_queue = None
        if isinstance(plugin, TriggerPlugin) and live.triggered_started:
            try:
                await plugin.stop()
            except Exception:
                log.exception("trigger plugin %s stop raised", plugin.name)
            live.triggered_started = False
        if isinstance(plugin, TemplatePlugin):
            for t in plugin.templates():
                tpl_registry.templates.pop(t.key, None)
        if isinstance(plugin, ConnectorPlugin):
            for c in plugin.connectors():
                conn_registry.connectors.pop(c.name, None)

        try:
            await plugin.on_unload()
        except Exception:
            log.exception("plugin %s on_unload raised", plugin.name)


    # --- config resolution ----------------------------------------------

    async def _resolve_config(
        self, s, plugin: Plugin, *, with_sources: bool = False
    ) -> tuple[dict, str]:
        """Compose the effective config from operator values + env + defaults.

        Returns `(values, misconfig_reason)`. When `with_sources=True`,
        the values dict is `{field_name: {"value": str, "source": str}}`
        for the UI; otherwise it's flat `{field_name: value}` for the
        plugin's own `configure()` call.
        """
        from ..models import PluginConfig

        try:
            schema: list[ConfigField] = plugin.config_schema()
        except Exception:
            log.exception("plugin %s config_schema raised", plugin.name)
            return ({}, "config_schema() raised")
        if not schema:
            return ({}, "")

        # Pull operator-set rows in one query.
        rows = (
            await s.execute(
                select(PluginConfig).where(PluginConfig.plugin_name == plugin.name)
            )
        ).scalars().all()
        operator_values = {r.key: r.value for r in rows}

        flat: dict = {}
        sourced: dict = {}
        missing_required: list[str] = []
        for f in schema:
            value = ""
            source = ""
            if f.name in operator_values and operator_values[f.name] != "":
                value = operator_values[f.name]
                source = "operator"
            elif f.env_fallback and os.getenv(f.env_fallback, ""):
                value = os.getenv(f.env_fallback, "")
                source = f"env:{f.env_fallback}"
            elif f.default:
                value = f.default
                source = "default"
            if f.required and not value:
                missing_required.append(f.name)
            flat[f.name] = value
            sourced[f.name] = {"value": value, "source": source}

        misconfig = ""
        if missing_required:
            misconfig = "missing required config: " + ", ".join(missing_required)
        return ((sourced if with_sources else flat), misconfig)

    async def _call_configure(self, plugin: Plugin, resolved: dict) -> None:
        try:
            await plugin.configure(resolved)
        except Exception:
            log.exception("plugin %s configure() raised", plugin.name)


async def _drive_notifier(plugin: NotifierPlugin, q: asyncio.Queue) -> None:
    try:
        while True:
            ev = await q.get()
            try:
                if plugin.matches(ev):
                    asyncio.create_task(_safe_dispatch(plugin, ev))
            except Exception:
                log.exception("plugin %s matches() raised", plugin.name)
    except asyncio.CancelledError:
        return


async def _safe_dispatch(plugin: NotifierPlugin, ev) -> None:
    try:
        await plugin.dispatch(ev)
    except Exception:
        log.exception("plugin %s dispatch raised", plugin.name)


_manager: PluginManager | None = None


def get_manager() -> PluginManager:
    global _manager
    if _manager is None:
        _manager = PluginManager()
    return _manager


def reset_manager() -> None:
    """Test helper."""
    global _manager
    _manager = None
