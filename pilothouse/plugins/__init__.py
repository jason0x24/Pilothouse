"""Plugin framework.

Pilothouse exposes five plugin kinds — Template, Connector, Notifier,
Trigger, Hook — discoverable through three paths:

  1. **In-tree builtins** — the templates/connectors that ship with the
     wheel. They use the same Plugin protocol as third-party plugins,
     so the API is uniform.
  2. **Entry points** — third-party `pip install pilothouse-foo` packages
     declaring `[project.entry-points."pilothouse.plugins"]`.
  3. **Directory scan** — drop `*.py` into `PILOTHOUSE_PLUGIN_DIR` (or
     `./plugins/`) for local dev iteration without packaging.

Operators control which plugins run via `pilothouse plugins enable/disable`
or the `/plugins` HTTP API. State is persisted in the `plugins` table
so disabled plugins stay disabled across restarts.
"""

from .base import (
    ConfigField,
    ConnectorPlugin,
    HookPlugin,
    NotifierPlugin,
    Plugin,
    PluginKind,
    PluginMeta,
    TemplatePlugin,
    TriggerPlugin,
)
from .manager import PluginManager, get_manager

__all__ = [
    "Plugin",
    "PluginMeta",
    "PluginKind",
    "ConfigField",
    "TemplatePlugin",
    "ConnectorPlugin",
    "NotifierPlugin",
    "HookPlugin",
    "TriggerPlugin",
    "PluginManager",
    "get_manager",
]
