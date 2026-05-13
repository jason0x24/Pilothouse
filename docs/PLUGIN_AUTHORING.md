# Plugin authoring guide

**English** · [简体中文](./PLUGIN_AUTHORING.zh-CN.md)

A complete walkthrough for writing, testing, and distributing a
Pilothouse plugin. Read in order if you've never written one; jump to
the section you need otherwise.

For **using** Pilothouse (not extending it), see [USER_GUIDE.md](./USER_GUIDE.md).

## 1. Five plugin kinds

| Kind | Subclass | When to use |
|---|---|---|
| `TemplatePlugin` | declares 1+ `Template`s | New agent playbook (e.g. a custom triage workflow) |
| `ConnectorPlugin` | declares 1+ `Connector`s | New external service tools (Jira, Discord webhook, Notion, …) |
| `NotifierPlugin` | `matches()` + `dispatch()` | Subscribe to the event bus, push to your channel |
| `TriggerPlugin` | `start()` + `stop()` lifecycle | New ways to fire agents (Kafka consumer, file watcher, …) |
| `HookPlugin` | `before_run` / `after_run` | Audit forwarding, custom metrics, per-tenant accounting |

A single plugin can mix kinds by inheriting multiple bases. For
example, a "GitHub Pro" plugin might contribute both new GitHub tools
*and* a template that uses them.

## 2. The 30-second start

```bash
# Generate a runnable starter + unit test.
pilothouse plugins scaffold notifier my_discord

# Discover it (drop into PILOTHOUSE_PLUGIN_DIR if not already).
PILOTHOUSE_PLUGIN_DIR=./plugins pilothouse plugins reload
pilothouse plugins info my_discord

# Configure required fields.
pilothouse plugins config set my_discord target https://discord.com/...
pilothouse plugins doctor    # exits 0 = healthy

# Run its tests.
pytest plugins/tests/test_my_discord.py
```

That's the entire loop. The scaffold output is intentionally
*runnable* — it's not a "TODO TODO TODO" outline, it works end-to-end
and is ready for you to edit.

## 3. Declared config schema

Every plugin can declare which configuration it needs:

```python
from pilothouse.plugins import ConfigField, NotifierPlugin, PluginMeta

class MyPlugin(NotifierPlugin):
    name = "my_plugin"

    def config_schema(self) -> list[ConfigField]:
        return [
            ConfigField(
                name="webhook_url",
                description="Where to send notifications.",
                required=True,            # missing → plugin marked misconfigured
                secret=True,              # masked in UI/CLI listings
                env_fallback="MY_PLUGIN_URL",  # PILOTHOUSE_… not required
            ),
            ConfigField(
                name="prefix",
                default="[bot]",
                description="Prefix every message with this.",
            ),
        ]

    async def configure(self, config: dict) -> None:
        # Called once after the schema is resolved, before on_load.
        self._url = config["webhook_url"]
        self._prefix = config["prefix"]
```

Resolution order at activation:
1. Operator-set value (`pilothouse plugins config set …` / `POST /plugins/{name}/config`)
2. `env_fallback` env var
3. `default`

Required fields whose resolved value is empty cause the plugin to be
flagged **misconfigured** — surfaced in `pilothouse plugins doctor`
and the `/plugins` console page. The plugin row stays in the listing
(so operators see what's broken) but is removed from the live
registries, so disabled-tools-can't-be-called is preserved.

## 4. Writing unit tests

`pilothouse.testing` exposes everything a plugin test needs:

```python
from pilothouse.testing import make_event, mock_tool_context, capture_events
from pilothouse.events import get_bus

# Notifier test:
async def test_notifier_matches_approval_events():
    p = MyPlugin()
    await p.configure({"webhook_url": "https://x", "prefix": "[bot]"})
    assert p.matches(make_event("approval_requested"))
    assert not p.matches(make_event("tool_call"))

# Connector test:
async def test_destructive_tool_short_circuits_in_dry_run():
    conn = MyConnectorPlugin().connectors()[0]
    tool = next(t for t in conn.tools() if t.name == "my_delete_thing")
    res = await tool.handler(mock_tool_context(dry_run=True), {"id": "abc"})
    assert res.content["dry_run"] is True

# Event-bus integration test:
async def test_event_capture():
    with capture_events() as events:
        get_bus().publish(make_event("custom", data={"x": 1}))
    assert events[0].kind == "custom"
```

For full integration tests (manager + DB + bus all wired up) use the
async `temp_plugin_manager` context:

```python
from pilothouse.testing import temp_plugin_manager

async def test_my_plugin_in_isolation():
    async with temp_plugin_manager(MyPlugin()) as mgr:
        # plugin is registered, activated, configured. Use mgr to
        # inspect listing / doctor / config; use the global bus /
        # registries to assert it integrated correctly.
        assert "my_plugin" in {p["name"] for p in mgr.list_plugins()}
```

## 5. Three distribution paths

### A. Directory drop (dev / private)

```bash
cp my_plugin.py $PILOTHOUSE_PLUGIN_DIR/
pilothouse plugins reload
```

Discovery imports the file under a fresh module name and picks up any
non-abstract `Plugin` subclass *defined in that file* (imported bases
are excluded).

### B. Pip package with entry point (sharing)

```toml
# pyproject.toml of your plugin package
[project]
name = "pilothouse-discord"
version = "0.1.0"

[project.entry-points."pilothouse.plugins"]
discord = "pilothouse_discord:DiscordNotifierPlugin"
```

After `pilothouse plugins install pilothouse-discord` (or `pip
install`), the plugin auto-discovers via the entry-point mechanism.
The CLI's `install` command calls pip + reload in one step:

```bash
pilothouse plugins install pilothouse-discord
pilothouse plugins install ./my-plugin                    # local sdist
pilothouse plugins install 'git+https://github.com/foo/bar'  # vcs
```

### C. In-tree (contribute back)

Open a PR that adds an `examples/plugins/…` file or, for built-ins,
contributes to `pilothouse/plugins/builtin.py`. Same Plugin API either
way — the only difference is where the code lives.

## 6. Cookbook: common patterns

### Per-tenant routing

```python
class TenantAwareNotifier(NotifierPlugin):
    def matches(self, event):
        # Only handle events for one tenant.
        return event.data.get("tenant_id") == self._target_tenant
```

### Suppress duplicate alerts

```python
class DedupedNotifier(NotifierPlugin):
    def __init__(self):
        self._recently_seen: set[str] = set()

    def matches(self, event):
        key = f"{event.kind}:{event.data.get('tool', '')}:{event.run_id[:8]}"
        if key in self._recently_seen:
            return False
        self._recently_seen.add(key)
        return True
```

### Audit-forwarding hook

The simplest version — but **not what production should ship**:

```python
class AuditForwarder(HookPlugin):
    async def after_run(self, *, run_id, agent_id, tenant_id, status, summary):
        async with httpx.AsyncClient() as client:
            await client.post(self._target, json={
                "run_id": run_id, "agent_id": agent_id, "tenant_id": tenant_id,
                "status": status, "summary": summary[:1000],
            })
```

Hooks run in the orchestration path, so a slow HTTP destination blocks
the agent. The reference implementation in
[`examples/plugins/siem_audit_forwarder.py`](../examples/plugins/siem_audit_forwarder.py)
shows the production-grade pattern: bounded in-memory queue +
background drainer + drop-oldest backpressure, so the SIEM endpoint
never gates Pilothouse latency.

### Long-poll trigger

```python
class KafkaTrigger(TriggerPlugin):
    async def start(self):
        self._task = asyncio.create_task(self._consume())

    async def stop(self):
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)

    async def _consume(self):
        from pilothouse.orchestration import execute_agent
        async for msg in self._kafka.subscribe(self._topic):
            await execute_agent(
                agent_id=self._agent_id,
                trigger=f"plugin:{self.name}",
                trigger_payload=msg.value,
            )
```

## 7. Plugins and the Temporal executor

Pilothouse can run with `PILOTHOUSE_TEMPORAL_ADDRESS` set, which moves
agent execution into Temporal workflows. **Most plugins don't notice**:

| Plugin kind | Affected by Temporal mode? |
|---|---|
| `TemplatePlugin` | No — templates are interpreted by the runtime, which is wrapped as activities |
| `ConnectorPlugin` | No — tool calls happen in the same activity as the runtime |
| `NotifierPlugin` | No — the event bus is shared across executors |
| `HookPlugin` | No — hooks fire from the orchestration layer, in either executor |
| `TriggerPlugin` | **Yes** — `start()` must call `pilothouse.orchestration.execute_agent`, which dispatches via the configured executor. Your code stays the same; the new run becomes a Temporal workflow automatically. |

In short: write the plugin once, the same code runs in-process or
backed by Temporal depending on deployment. No conditional code.

## 8. Operational concerns

| Concern | Guidance |
|---|---|
| **Errors in dispatch** | The manager catches and logs; your `dispatch` / `handler` should be defensive but doesn't need a top-level try/except. |
| **Slow downstreams** | A hook / notifier that does slow HTTP blocks the orchestration path. Use a bounded queue + background drainer — see [`siem_audit_forwarder.py`](../examples/plugins/siem_audit_forwarder.py) for the pattern. |
| **Secrets at rest** | Stored plain in `plugin_configs.value`. Encrypt at the volume / DB layer for production deployments. UI/CLI mask by default. |
| **Compatibility** | `PluginMeta.pilothouse_compat` is informational today; future major versions will refuse to load incompatible specifiers. |
| **Stateful plugins** | Stash on `self` in `configure()`. The manager constructs the plugin once per process; multiple `enable/disable` cycles call `on_load` / `on_unload` but don't re-instantiate. |
| **Threading** | Everything's asyncio. Don't block in handlers (no `time.sleep`, no sync HTTP). Use `httpx.AsyncClient`. |
| **CI gate** | `pilothouse plugins doctor` exits non-zero if any plugin is enabled but misconfigured. Run it in your pre-deploy check. |

## 9. Reference

- Base classes: `pilothouse.plugins.{Plugin, TemplatePlugin, ConnectorPlugin, NotifierPlugin, TriggerPlugin, HookPlugin, ConfigField, PluginMeta}`
- Testing helpers: `pilothouse.testing.{mock_tool_context, make_event, capture_events, temp_plugin_manager}`
- Manager (rarely used directly): `pilothouse.plugins.PluginManager`, `pilothouse.plugins.get_manager()`
- Example plugins:
  - [`discord_notifier.py`](../examples/plugins/discord_notifier.py) — NotifierPlugin with config schema
  - [`poll_url_trigger.py`](../examples/plugins/poll_url_trigger.py) — TriggerPlugin with start/stop lifecycle
  - [`siem_audit_forwarder.py`](../examples/plugins/siem_audit_forwarder.py) — HookPlugin with bounded-queue backpressure
