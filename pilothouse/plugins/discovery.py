"""Plugin discovery — three independent paths.

Each discoverer returns `list[Plugin]` plus a `source` string for the
manager to record. Errors during discovery are logged but never raised
— a single broken plugin must not stop the rest of the system from
loading.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import logging
import os
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .base import Plugin

log = logging.getLogger(__name__)


@dataclass
class Discovered:
    plugin: Plugin
    source: str  # "builtin" | "entry_point:<dist>" | "directory:<path>"


# --- in-tree builtins ----------------------------------------------------


def discover_builtins() -> list[Discovered]:
    """The plugins shipped inside the Pilothouse wheel.

    Imported lazily so a stripped-down deployment that wants to disable
    every built-in doesn't pay the import cost. The actual contributing
    classes live in `pilothouse.plugins.builtin`.
    """
    out: list[Discovered] = []
    try:
        from . import builtin

        for plugin in builtin.builtin_plugins():
            out.append(Discovered(plugin=plugin, source="builtin"))
    except Exception:  # pragma: no cover — wheel-internal, should never fail
        log.exception("failed to discover builtin plugins")
    return out


# --- entry-point discovery ----------------------------------------------


def discover_entry_points(group: str = "pilothouse.plugins") -> list[Discovered]:
    """Scan installed packages for `[project.entry-points."pilothouse.plugins"]`.

    Each entry point should resolve to a Plugin subclass *or* a callable
    that returns a Plugin instance. We accept both.

      [project.entry-points."pilothouse.plugins"]
      discord_notifier = "my_pkg.plugin:DiscordPlugin"
      jira_connector   = "my_pkg.jira:make_plugin"
    """
    from importlib.metadata import entry_points

    out: list[Discovered] = []
    try:
        eps = entry_points(group=group)
    except TypeError:  # pragma: no cover — older importlib.metadata API
        eps_all = entry_points()
        eps = eps_all.get(group, [])  # type: ignore[union-attr]

    for ep in eps:
        try:
            obj = ep.load()
            instance = obj() if callable(obj) and not _is_plugin_class(obj) else (
                obj() if _is_plugin_class(obj) else obj
            )
            if not isinstance(instance, Plugin):
                log.warning(
                    "entry point %s did not yield a Plugin instance (got %r)",
                    ep.name, type(instance).__name__,
                )
                continue
            dist = getattr(ep, "dist", None)
            dist_name = getattr(dist, "name", "") if dist else ""
            out.append(
                Discovered(plugin=instance, source=f"entry_point:{dist_name or ep.name}")
            )
        except Exception:
            log.exception("failed to load entry-point plugin %s", ep.name)
    return out


def _is_plugin_class(obj) -> bool:
    if not inspect.isclass(obj):
        return False
    if not issubclass(obj, Plugin) or obj is Plugin:
        return False
    # Skip abstract bases (TemplatePlugin, ConnectorPlugin, …) — they
    # show up in any module that imports them, but we only want
    # concrete plugin classes.
    if getattr(obj, "__abstractmethods__", frozenset()):
        return False
    return True


# --- directory discovery ------------------------------------------------


def discover_directory(directory: Path | str | None = None) -> list[Discovered]:
    """Import every `*.py` file in a directory and collect Plugin subclasses.

    Resolution order for the directory:
      1. Explicit `directory=` argument
      2. `PILOTHOUSE_PLUGIN_DIR` env var
      3. `./plugins/` relative to cwd, if it exists
    """
    if directory is None:
        env = os.getenv("PILOTHOUSE_PLUGIN_DIR")
        if env:
            directory = env
        elif Path("plugins").is_dir():
            directory = "plugins"
        else:
            return []
    path = Path(directory)
    if not path.is_dir():
        return []

    out: list[Discovered] = []
    for py in sorted(path.glob("*.py")):
        if py.name.startswith("_"):
            continue
        mod_name = f"_pilothouse_plugin_{py.stem}"
        try:
            spec = importlib.util.spec_from_file_location(mod_name, py)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)
        except Exception:
            log.warning(
                "directory plugin %s failed to import:\n%s",
                py, traceback.format_exc()[:1000],
            )
            continue

        for _, obj in inspect.getmembers(module, _is_plugin_class):
            # Only register classes *defined in this file* — skip classes
            # the file merely imports (e.g. abstract bases imported from
            # pilothouse.plugins).
            if getattr(obj, "__module__", "") != mod_name:
                continue
            try:
                instance = obj()
            except Exception:
                log.exception("instantiating plugin class %s failed", obj)
                continue
            if not instance.name:
                # Auto-name from the class so authors don't need to set it
                # explicitly for simple cases.
                instance.name = obj.__name__.lower()
            out.append(Discovered(plugin=instance, source=f"directory:{py}"))
    return out


def discover_all() -> Iterable[Discovered]:
    """All three paths, in priority order. The manager dedupes by name —
    later discoveries with the same `name` are ignored, so an entry-point
    plugin can't be silently replaced by a directory drop unless the
    operator removes it first."""
    yield from discover_builtins()
    yield from discover_entry_points()
    yield from discover_directory()
