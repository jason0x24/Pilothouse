"""Scaffolds for `pilothouse plugins scaffold`.

Each kind has a complete, runnable template — copy-paste the output
into `PILOTHOUSE_PLUGIN_DIR`, `pilothouse plugins reload`, and you're
live. The scaffolds also include a `tests/` companion file so authors
have a starting point for unit tests.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Scaffold:
    plugin_filename: str   # relative to plugin_dir
    plugin_body: str
    test_filename: str     # relative to plugin_dir/tests
    test_body: str


VALID_KINDS = ("notifier", "connector", "template", "trigger", "hook")


def render(kind: str, name: str) -> Scaffold:
    if kind not in VALID_KINDS:
        raise ValueError(
            f"unknown plugin kind {kind!r}; expected one of {', '.join(VALID_KINDS)}"
        )
    if not name.replace("_", "").isalnum():
        raise ValueError("plugin name must be alphanumeric + underscore only")
    class_name = "".join(part.capitalize() for part in name.split("_")) + "Plugin"
    return _RENDERERS[kind](name, class_name)


# --- per-kind renderers ------------------------------------------------


def _notifier(name: str, class_name: str) -> Scaffold:
    body = f'''"""{name} — notifier plugin scaffold.

Edit `matches()` to filter the events you care about and `dispatch()`
to do the I/O. Test with: `pytest tests/test_{name}.py`.
"""

from __future__ import annotations

from pilothouse.events import RunEvent
from pilothouse.plugins import ConfigField, NotifierPlugin, PluginMeta


class {class_name}(NotifierPlugin):
    name = "{name}"

    def __init__(self) -> None:
        self._target = ""

    def meta(self) -> PluginMeta:
        return PluginMeta(
            name=self.name,
            version="0.1.0",
            description="TODO: describe what this notifier does.",
            kinds=set(self._inferred_kinds()),
        )

    def config_schema(self) -> list[ConfigField]:
        return [
            ConfigField(
                name="target",
                description="TODO: where to send notifications.",
                required=True,
                secret=True,
                env_fallback="{name.upper()}_TARGET",
            ),
        ]

    async def configure(self, config: dict) -> None:
        self._target = config.get("target", "").strip()

    def matches(self, event: RunEvent) -> bool:
        # TODO: filter to the kinds you actually care about.
        return event.kind in {{"approval_requested", "run_terminal"}}

    async def dispatch(self, event: RunEvent) -> None:
        if not self._target:
            return
        # TODO: do the actual delivery (httpx.post, etc.).
        # Be defensive — never raise out of dispatch; the manager logs
        # but swallowing here keeps surface tiny.
        print(f"[{name}] would dispatch {{event.kind}} for run {{event.run_id[:8]}}")
'''
    test = f'''"""Unit tests for {name}."""

from __future__ import annotations

import pytest

from pilothouse.testing import make_event
from {name} import {class_name}


@pytest.fixture
async def plugin():
    p = {class_name}()
    await p.configure({{"target": "https://example.invalid/x"}})
    return p


def test_matches_approval_requested(plugin) -> None:
    assert plugin.matches(make_event("approval_requested"))
    assert not plugin.matches(make_event("tool_call"))


async def test_dispatch_no_op_without_target() -> None:
    p = {class_name}()
    # No configure() call → target stays empty → dispatch returns silently.
    await p.dispatch(make_event("approval_requested"))
'''
    return Scaffold(
        plugin_filename=f"{name}.py",
        plugin_body=body,
        test_filename=f"test_{name}.py",
        test_body=test,
    )


def _connector(name: str, class_name: str) -> Scaffold:
    body = f'''"""{name} — connector plugin scaffold.

A connector contributes one Connector object that bundles tools.
Tools marked `is_destructive=True` go through Pilothouse's dry-run
and approval gates automatically.
"""

from __future__ import annotations

from pilothouse.connectors.base import Connector, ToolContext, ToolResult
from pilothouse.plugins import ConfigField, ConnectorPlugin, PluginMeta


class _{class_name}Connector(Connector):
    name = "{name}"

    def __init__(self) -> None:
        super().__init__()
        self._add(
            "{name}_get_thing",
            "Read a thing by id (non-destructive).",
            {{
                "type": "object",
                "properties": {{"id": {{"type": "string"}}}},
                "required": ["id"],
            }},
            self._get_thing,
        )
        self._add(
            "{name}_delete_thing",
            "Delete a thing by id. DESTRUCTIVE — gated by dry-run / approval.",
            {{
                "type": "object",
                "properties": {{"id": {{"type": "string"}}}},
                "required": ["id"],
            }},
            self._delete_thing,
            is_destructive=True,
        )

    async def _get_thing(self, ctx: ToolContext, params: dict) -> ToolResult:
        # TODO: real read implementation.
        return ToolResult(content={{"id": params["id"], "stub": True}})

    async def _delete_thing(self, ctx: ToolContext, params: dict) -> ToolResult:
        if ctx.dry_run:
            return ToolResult(
                content={{"dry_run": True, "would_delete": params["id"]}}
            )
        # TODO: real delete implementation.
        return ToolResult(content={{"deleted": params["id"]}})


class {class_name}(ConnectorPlugin):
    name = "{name}"

    def meta(self) -> PluginMeta:
        return PluginMeta(
            name=self.name,
            version="0.1.0",
            description="TODO: describe this connector.",
            kinds=set(self._inferred_kinds()),
        )

    def config_schema(self) -> list[ConfigField]:
        return [
            ConfigField(
                name="api_token",
                description="TODO: API credential.",
                required=False,
                secret=True,
                env_fallback="{name.upper()}_API_TOKEN",
            ),
        ]

    def connectors(self) -> list[Connector]:
        return [_{class_name}Connector()]
'''
    test = f'''"""Unit tests for the {name} connector."""

from __future__ import annotations

from pilothouse.testing import mock_tool_context
from {name} import {class_name}


async def test_get_thing_returns_payload() -> None:
    conn = {class_name}().connectors()[0]
    tool = next(t for t in conn.tools() if t.name == "{name}_get_thing")
    res = await tool.handler(mock_tool_context(dry_run=False), {{"id": "abc"}})
    assert res.content["id"] == "abc"


async def test_delete_thing_dry_run_short_circuits() -> None:
    conn = {class_name}().connectors()[0]
    tool = next(t for t in conn.tools() if t.name == "{name}_delete_thing")
    res = await tool.handler(mock_tool_context(dry_run=True), {{"id": "abc"}})
    assert res.content["dry_run"] is True
    assert res.content["would_delete"] == "abc"
'''
    return Scaffold(
        plugin_filename=f"{name}.py",
        plugin_body=body,
        test_filename=f"test_{name}.py",
        test_body=test,
    )


def _template(name: str, class_name: str) -> Scaffold:
    body = f'''"""{name} — template plugin scaffold.

A template turns a trigger payload into the inputs the runtime needs:
prompt, user message, allowed tools. `mock_plan` powers the mock-mode
test suite — you can run the template end-to-end without LLM credits.
"""

from __future__ import annotations

import json

from pilothouse.plugins import PluginMeta, TemplatePlugin
from pilothouse.templates.base import Template, TemplatePlan


SYSTEM_PROMPT = """You are a TODO agent. Describe the workflow here, including:
  * the steps the model should take
  * the report format expected
  * rules and constraints
"""


class _{class_name}Template(Template):
    key = "{name}"
    name = "{class_name}"
    description = "TODO: one-line description."
    default_tools = []  # e.g. ["github", "datadog"]

    def plan(self, *, trigger_payload: dict, params: dict) -> TemplatePlan:
        user_message = (
            f"TODO: build the user message from the trigger.\\n"
            f"Trigger payload:\\n```json\\n{{json.dumps(trigger_payload, indent=2)[:2000]}}\\n```"
        )
        return TemplatePlan(
            system_prompt=SYSTEM_PROMPT,
            user_message=user_message,
            tool_names=params.get("tool_names") or self.default_tools,
        )

    def mock_plan(self, *, trigger_payload: dict, params: dict) -> list[dict]:
        # Each step is either {{"tool": ..., "input": ...}} or {{"final": ...}}.
        return [
            {{"final": "TODO: produce the agent's final text output."}},
        ]


class {class_name}(TemplatePlugin):
    name = "{name}"

    def meta(self) -> PluginMeta:
        return PluginMeta(
            name=self.name,
            version="0.1.0",
            description="TODO: what this template automates.",
            kinds=set(self._inferred_kinds()),
        )

    def templates(self) -> list[Template]:
        return [_{class_name}Template()]
'''
    test = f'''"""Unit tests for the {name} template."""

from __future__ import annotations

from {name} import {class_name}, _{class_name}Template


def test_plan_includes_trigger_payload() -> None:
    tpl = _{class_name}Template()
    plan = tpl.plan(trigger_payload={{"alert_id": "abc"}}, params={{}})
    assert "abc" in plan.user_message
    assert plan.tool_names == tpl.default_tools


def test_mock_plan_terminates() -> None:
    tpl = _{class_name}Template()
    steps = tpl.mock_plan(trigger_payload={{}}, params={{}})
    assert any("final" in s for s in steps)
'''
    return Scaffold(
        plugin_filename=f"{name}.py",
        plugin_body=body,
        test_filename=f"test_{name}.py",
        test_body=test,
    )


def _trigger(name: str, class_name: str) -> Scaffold:
    body = f'''"""{name} — trigger plugin scaffold.

A trigger plugin owns its own background task. Implement `start()` to
spawn whatever consumer/poller you need; `stop()` must tear it down
cleanly so enable/disable round-trips don't leak tasks.
"""

from __future__ import annotations

import asyncio
import logging

from pilothouse.plugins import ConfigField, PluginMeta, TriggerPlugin

log = logging.getLogger(__name__)


class {class_name}(TriggerPlugin):
    name = "{name}"

    def __init__(self) -> None:
        self._agent_id = ""
        self._interval = 60
        self._task: asyncio.Task | None = None

    def meta(self) -> PluginMeta:
        return PluginMeta(
            name=self.name,
            version="0.1.0",
            description="TODO: describe the trigger source.",
            kinds=set(self._inferred_kinds()),
        )

    def config_schema(self) -> list[ConfigField]:
        return [
            ConfigField(name="agent_id", required=True,
                        description="Pilothouse agent to fire when the trigger source emits."),
            ConfigField(name="interval_seconds", default="60",
                        description="Polling interval; ignored if your source is push-based."),
        ]

    async def configure(self, config: dict) -> None:
        self._agent_id = config.get("agent_id", "").strip()
        try:
            self._interval = max(5, int(config.get("interval_seconds", "60") or "60"))
        except ValueError:
            self._interval = 60

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        if not self._agent_id:
            return  # manager already flagged misconfig
        self._task = asyncio.create_task(self._run(), name=f"trigger-{{self.name}}")

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
                # TODO: replace this with a real source (Kafka consumer,
                # SQS poll, file watcher, etc.). When the source emits,
                # call execute_agent with the appropriate payload.
                await asyncio.sleep(self._interval)
                # await execute_agent(agent_id=self._agent_id,
                #                     trigger=f"plugin:{{self.name}}",
                #                     trigger_payload={{...}})
        except asyncio.CancelledError:
            return
'''
    test = f'''"""Unit tests for the {name} trigger plugin."""

from __future__ import annotations

import asyncio

from {name} import {class_name}


async def test_start_stop_cycle_is_clean() -> None:
    p = {class_name}()
    await p.configure({{"agent_id": "fake-agent", "interval_seconds": "5"}})
    await p.start()
    assert p._task is not None and not p._task.done()
    await p.stop()
    assert p._task is None


async def test_no_agent_id_does_not_start() -> None:
    p = {class_name}()
    await p.configure({{"agent_id": ""}})
    await p.start()
    assert p._task is None
'''
    return Scaffold(
        plugin_filename=f"{name}.py",
        plugin_body=body,
        test_filename=f"test_{name}.py",
        test_body=test,
    )


def _hook(name: str, class_name: str) -> Scaffold:
    body = f'''"""{name} — hook plugin scaffold.

Hook plugins observe every Run start / finish. Useful for forwarding
audit data to a SIEM, custom metrics collectors, or per-tenant
billing accumulators. Hooks must be quick — they run synchronously
in the orchestration path.
"""

from __future__ import annotations

import logging

from pilothouse.plugins import ConfigField, HookPlugin, PluginMeta

log = logging.getLogger(__name__)


class {class_name}(HookPlugin):
    name = "{name}"

    def __init__(self) -> None:
        self._target = ""

    def meta(self) -> PluginMeta:
        return PluginMeta(
            name=self.name,
            version="0.1.0",
            description="TODO: forward run lifecycle to <where>.",
            kinds=set(self._inferred_kinds()),
        )

    def config_schema(self) -> list[ConfigField]:
        return [
            ConfigField(name="target", required=False, secret=True,
                        env_fallback="{name.upper()}_TARGET",
                        description="Where to forward run data."),
        ]

    async def configure(self, config: dict) -> None:
        self._target = config.get("target", "").strip()

    async def before_run(self, *, run_id: str, agent_id: str, tenant_id: str) -> None:
        # TODO: emit "run started" to your destination.
        log.debug("{name}: before_run %s for agent %s tenant %s",
                  run_id, agent_id, tenant_id)

    async def after_run(
        self, *, run_id: str, agent_id: str, tenant_id: str, status: str, summary: str
    ) -> None:
        # TODO: emit "run finished" with status/summary.
        log.debug("{name}: after_run %s status=%s", run_id, status)
'''
    test = f'''"""Unit tests for the {name} hook plugin."""

from __future__ import annotations

from {name} import {class_name}


async def test_lifecycle_hooks_are_no_op_without_target() -> None:
    p = {class_name}()
    # Default config = no target → hooks should not raise.
    await p.before_run(run_id="r", agent_id="a", tenant_id="t")
    await p.after_run(run_id="r", agent_id="a", tenant_id="t", status="succeeded", summary="ok")
'''
    return Scaffold(
        plugin_filename=f"{name}.py",
        plugin_body=body,
        test_filename=f"test_{name}.py",
        test_body=test,
    )


_RENDERERS = {
    "notifier": _notifier,
    "connector": _connector,
    "template": _template,
    "trigger": _trigger,
    "hook": _hook,
}
