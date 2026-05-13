# Example plugins

Drop-in examples for the three Pilothouse plugin discovery paths.

| Path | Use when | Example here |
|---|---|---|
| Directory | Local dev iteration; private / one-off plugins | `discord_notifier.py` |
| Entry points | Publishing as a pip package on PyPI / your private index | see `entrypoint_example/` |
| Built-in | Vendoring into core (PR welcome) | `pilothouse/plugins/builtin.py` |

## Run the directory example

```bash
# 1. Point Pilothouse at this directory.
export PILOTHOUSE_PLUGIN_DIR=$(pwd)
export DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/.../...

# 2. Re-discover.
pilothouse plugins reload
pilothouse plugins list
# discord_notifier  notifier  0.1.0  on  directory:.../discord_notifier.py

# 3. Trigger any agent in live mode; failures and approval requests
#    now post to Discord.
```

## Write your own

The five plugin kinds:

| Kind | Base class | Contributes |
|---|---|---|
| Template | `TemplatePlugin` | New agent templates (`templates()` returns `Template` instances) |
| Connector | `ConnectorPlugin` | New connectors with tools (`connectors()` returns `Connector` instances) |
| Notifier | `NotifierPlugin` | Subscribers to the event bus (`matches()` + `dispatch()`) |
| Trigger | `TriggerPlugin` | New ways to fire agents (`start()` / `stop()` lifecycle) |
| Hook | `HookPlugin` | `before_run` / `after_run` callbacks for audit forwarding |

A plugin can mix kinds by inheriting multiple bases.

Minimum skeleton:

```python
from pilothouse.plugins import NotifierPlugin, PluginMeta
from pilothouse.events import RunEvent

class MyPlugin(NotifierPlugin):
    name = "my_plugin"

    def meta(self) -> PluginMeta:
        return PluginMeta(
            name=self.name,
            version="0.1.0",
            description="One-line description.",
            kinds=set(self._inferred_kinds()),
        )

    def matches(self, event: RunEvent) -> bool:
        return event.kind == "approval_requested"

    async def dispatch(self, event: RunEvent) -> None:
        # do something with the event
        ...
```

## Publishing as a pip package

```toml
# pyproject.toml of your plugin package
[project]
name = "pilothouse-discord"
version = "0.1.0"

[project.entry-points."pilothouse.plugins"]
discord = "pilothouse_discord:DiscordNotifierPlugin"
```

After `pip install pilothouse-discord`, Pilothouse auto-discovers the
plugin via the entry-point mechanism — no env vars or directory
copying needed. `pilothouse plugins list` will show it sourced as
`entry_point:pilothouse-discord`.
