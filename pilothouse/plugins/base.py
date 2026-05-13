"""Plugin base classes.

Each plugin kind has a tiny abstract surface — just enough for the
manager to know what to register where. Implementations supply zero or
more contributions of their kind. A single plugin can mix kinds (e.g. a
"GitHub Pro" plugin contributing both extra GitHub tools and a new
template that uses them) by inheriting multiple bases.
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Awaitable, Callable

if TYPE_CHECKING:
    from ..connectors.base import Connector
    from ..events import RunEvent
    from ..templates.base import Template


class PluginKind(str, enum.Enum):
    template = "template"
    connector = "connector"
    notifier = "notifier"
    trigger = "trigger"
    hook = "hook"


@dataclass
class PluginMeta:
    """Metadata exposed by each plugin instance.

    `source` is set by the manager when the plugin is discovered:
    "builtin" / "entry_point" / "directory:<path>" — surface in
    `pilothouse plugins list` and the HTTP listing.
    """

    name: str
    version: str = "0.0.0"
    description: str = ""
    pilothouse_compat: str = ">=0.1.0"  # informational; checked at load
    source: str = "unknown"
    kinds: set[PluginKind] = field(default_factory=set)


@dataclass
class ConfigField:
    """One configurable knob exposed by a plugin's `config_schema()`.

    Resolution order at activation time:
      1. Operator-set value in `plugin_configs` table.
      2. Env var named by `env_fallback`.
      3. `default`.
    Required fields whose resolved value is empty cause the plugin to
    be flagged `misconfigured` — it stays out of the live registry
    until fixed. Surfaced in `pilothouse plugins doctor` and the UI.

    `secret=True` means the value is masked in listings; storage is
    plain JSON (encrypt at the volume / DB layer in production).
    """

    name: str
    description: str = ""
    required: bool = False
    secret: bool = False
    default: str = ""
    env_fallback: str = ""


class Plugin(ABC):
    """Base class for every plugin.

    Subclasses set the class attribute `name` (uniquely identifies the
    plugin in CRUD APIs) and override `meta()` to declare versioning +
    contributed kinds. The lifecycle hooks default to no-ops, and most
    plugins won't need to override them.
    """

    name: str = ""

    def meta(self) -> PluginMeta:
        return PluginMeta(name=self.name, kinds=set(self._inferred_kinds()))

    def config_schema(self) -> list[ConfigField]:
        """Declare the configuration the plugin needs.

        Override to expose required / optional knobs. The default is
        no fields — the plugin works out of the box. Built-ins read
        their config from process-wide env vars, so they leave this
        empty too.
        """
        return []

    async def configure(self, config: dict) -> None:
        """Receive the resolved config dict (field name → value).

        Called once after the schema is resolved, before `on_load`.
        Plugins typically stash values on `self` for later use by
        `dispatch` / handler methods. The default is a no-op so
        plugins without config can ignore this hook.
        """
        return None

    def _inferred_kinds(self) -> list[PluginKind]:
        kinds: list[PluginKind] = []
        if isinstance(self, TemplatePlugin):
            kinds.append(PluginKind.template)
        if isinstance(self, ConnectorPlugin):
            kinds.append(PluginKind.connector)
        if isinstance(self, NotifierPlugin):
            kinds.append(PluginKind.notifier)
        if isinstance(self, TriggerPlugin):
            kinds.append(PluginKind.trigger)
        if isinstance(self, HookPlugin):
            kinds.append(PluginKind.hook)
        return kinds

    async def on_load(self) -> None:
        """Called once when the plugin is enabled by the manager."""
        return None

    async def on_unload(self) -> None:
        """Called when the plugin is disabled or the process shuts down."""
        return None


class TemplatePlugin(Plugin):
    """Contributes one or more agent templates."""

    @abstractmethod
    def templates(self) -> list["Template"]:
        ...


class ConnectorPlugin(Plugin):
    """Contributes one or more connectors (each may expose many tools)."""

    @abstractmethod
    def connectors(self) -> list["Connector"]:
        ...


class NotifierPlugin(Plugin):
    """Subscribes to the run-event bus and dispatches notifications.

    Plugins implement `matches` (sync, cheap filter) and `dispatch`
    (async, may do IO). The manager wires every enabled NotifierPlugin
    to the bus on load and tears the subscription down on unload.
    """

    @abstractmethod
    def matches(self, event: "RunEvent") -> bool:
        ...

    @abstractmethod
    async def dispatch(self, event: "RunEvent") -> None:
        ...


class TriggerPlugin(Plugin):
    """Adds a new way to fire agents.

    Triggers are typically background tasks (long-poll consumers,
    schedulers) the plugin owns. The manager calls `start()` on load
    and `stop()` on unload; the plugin is responsible for calling
    `pilothouse.orchestration.execute_agent(...)` when its source
    fires.
    """

    @abstractmethod
    async def start(self) -> None:
        ...

    @abstractmethod
    async def stop(self) -> None:
        ...


class HookPlugin(Plugin):
    """Lifecycle hooks invoked around each Run.

    Use cases: forwarding the audit log to an SIEM, pushing run-level
    metrics to a custom collector, custom cost accounting. Hooks run
    synchronously in the orchestration path; they should be quick and
    not raise — exceptions are caught and logged but never bubble.
    """

    async def before_run(self, *, run_id: str, agent_id: str, tenant_id: str) -> None:
        return None

    async def after_run(
        self, *, run_id: str, agent_id: str, tenant_id: str, status: str, summary: str
    ) -> None:
        return None


# Convenience type for entry-point factory functions.
PluginFactory = Callable[[], Plugin]


__all__ = [
    "Plugin",
    "PluginKind",
    "PluginMeta",
    "ConfigField",
    "TemplatePlugin",
    "ConnectorPlugin",
    "NotifierPlugin",
    "TriggerPlugin",
    "HookPlugin",
    "PluginFactory",
]
