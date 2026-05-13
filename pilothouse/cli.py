"""Pilothouse CLI.

Subcommands:

  pilothouse serve                          — run HTTP server + scheduler
  pilothouse db init                        — create tables
  pilothouse templates list                 — show built-in templates
  pilothouse connectors list                — show built-in connectors
  pilothouse agents create <name> <tpl>     — register an agent
  pilothouse agents list                    — list agents
  pilothouse agents show <id>               — show an agent + recent runs
  pilothouse agents trigger <id>            — manual run (reads JSON payload from stdin/--file)
  pilothouse agents delete <id>             — remove an agent
  pilothouse runs show <id>                 — show run details + events
  pilothouse demo                           — bootstrap one of each agent + trigger them
"""

from __future__ import annotations

from typing import Any

import asyncio
import json
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.json import JSON as RichJSON
from rich.table import Table
from sqlalchemy import select

from .connectors import register_builtin_connectors
from .connectors.base import registry as conn_registry
from .connectors.mcp import McpServerSpec, parse_command, register_mcp_server, unregister_mcp_server
from .db import init_db, session
from .declarative import apply_plan, compute_plan, load_manifest, render_plan
from .plugins.manager import get_manager as get_plugin_manager
from .tenants import (
    add_api_key,
    create_tenant,
    delete_tenant,
    ensure_default_tenant,
    list_tenants,
    remove_api_key,
    set_quota,
)
from .models import Agent, Approval, ApprovalStatus, Event, McpServer, Run, RunStatus
from .orchestration.executor import (
    cancel_run,
    execute_agent,
    resume_run,
    retry_run,
    sweep_expired_approvals,
)
from .templates import register_builtin_templates
from .templates.base import registry as tpl_registry

console = Console()


# pytest config dropped into the scaffold directory so `pytest tests/`
# works out of the box without the user discovering pytest-asyncio
# config through trial & error. Delete if you already have a
# pyproject.toml [tool.pytest.ini_options] block with asyncio_mode.
_SCAFFOLD_PYTEST_INI = """\
[pytest]
asyncio_mode = auto
"""


def _bootstrap() -> None:
    register_builtin_connectors()
    register_builtin_templates()


# --- helpers --------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


# --- root group -----------------------------------------------------------


@click.group()
def main() -> None:
    """Pilothouse — AI DevOps Copilot platform."""
    _bootstrap()


@main.command()
@click.option("--host", default=None)
@click.option("--port", default=None, type=int)
def serve(host: str | None, port: int | None) -> None:
    """Run the HTTP server (uvicorn) — management API + webhook receivers."""
    from .config import get_settings

    s = get_settings()
    import uvicorn

    uvicorn.run(
        "pilothouse.api.server:build_app",
        host=host or s.host,
        port=port or s.port,
        factory=True,
        log_level=s.log_level.lower(),
    )


# --- db -------------------------------------------------------------------


@main.group()
def db() -> None:
    """Database management."""


@db.command("init")
def db_init() -> None:
    """Create tables."""
    _run(init_db())
    console.print("[green]OK[/] database initialised")


# --- templates / connectors ----------------------------------------------


@main.group()
def templates() -> None:
    """Built-in templates."""


@templates.command("list")
def templates_list() -> None:
    """List currently *enabled* templates.

    Goes through the plugin manager so that any template whose owning
    plugin is disabled (`pilothouse plugins disable …`) doesn't appear.
    """

    async def _go() -> None:
        await init_db()
        await get_plugin_manager().load_all()
        table = Table(title="Templates (enabled)")
        table.add_column("key")
        table.add_column("name")
        table.add_column("default tools")
        table.add_column("description")
        for t in tpl_registry.all():
            table.add_row(t.key, t.name, ", ".join(t.default_tools), t.description)
        console.print(table)

    _run(_go())


@main.group()
def providers() -> None:
    """LLM provider configuration."""


@providers.command("list")
def providers_list() -> None:
    """Show every registered provider and whether it's currently usable.

    Active provider is highlighted. Operators choose models per-provider
    using whatever id strings that provider accepts — Pilothouse does
    not maintain a model whitelist. Examples:

      anthropic   → claude-opus-4-5, claude-sonnet-4-5, …
      openai      → gpt-4o, gpt-4o-mini, o3-mini, …
      openrouter  → anthropic/claude-sonnet-4-5, openai/gpt-4o, …
    """
    from .agent.providers import (
        PROVIDER_FACTORIES,
        _PROVIDER_KEY_FIELDS,
        _resolve_provider_name,
    )
    from .config import get_settings

    s = get_settings()
    active = _resolve_provider_name(s)

    table = Table(title="LLM providers")
    for col in ("provider", "credential env var", "configured?", "active?"):
        table.add_column(col)
    for name in sorted(PROVIDER_FACTORIES):
        key_field = _PROVIDER_KEY_FIELDS.get(name)
        env_name = f"PILOTHOUSE_{key_field.upper()}" if key_field else "(none — built-in)"
        has_key = bool(getattr(s, key_field, "")) if key_field else True
        configured = "[green]yes[/]" if has_key else "[dim]no[/]"
        is_active = "[bold green]✓[/]" if name == active else ""
        table.add_row(name, env_name, configured, is_active)
    console.print(table)
    console.print(
        f"\nResolved provider for this process: [bold]{active}[/]   "
        f"(planner={s.model_planner!r}, worker={s.model_worker!r})"
    )


@providers.command("doctor")
def providers_doctor() -> None:
    """Verify the configured provider actually has a credential.

    Exits non-zero if not — handy as a pre-deploy CI gate.
    """
    from .agent.providers import (
        PROVIDER_FACTORIES,
        _PROVIDER_KEY_FIELDS,
        _resolve_provider_name,
    )
    from .config import get_settings

    s = get_settings()
    name = _resolve_provider_name(s)
    if name not in PROVIDER_FACTORIES:
        raise click.ClickException(f"unknown provider {name!r}")

    if name == "mock":
        console.print(
            "[yellow]using mock provider[/] — no real LLM is being called. "
            "Set PILOTHOUSE_ANTHROPIC_API_KEY / OPENAI_API_KEY / "
            "OPENROUTER_API_KEY to enable real calls."
        )
        return

    key_field = _PROVIDER_KEY_FIELDS.get(name)
    if key_field and not getattr(s, key_field, ""):
        env_name = f"PILOTHOUSE_{key_field.upper()}"
        raise click.ClickException(
            f"provider {name!r} selected but {env_name} is empty."
        )
    console.print(f"[green]✓[/] provider={name!r}  planner={s.model_planner!r}  worker={s.model_worker!r}")


@main.group()
def connectors() -> None:
    """Built-in connectors."""


@connectors.command("add-mcp")
@click.argument("name")
@click.argument("command", nargs=-1, required=False)
@click.option("--env", "env_pairs", multiple=True, help="KEY=VAL, repeatable")
@click.option(
    "--destructive",
    "destructive",
    multiple=True,
    help="Mark a specific MCP tool name as destructive (repeatable)",
)
@click.option("--description", default="")
@click.option("--http", "http_url", default=None, help="Use HTTP transport against this URL")
@click.option(
    "--header",
    "header_pairs",
    multiple=True,
    help="HTTP transport: NAME=VALUE auth header (repeatable)",
)
def connectors_add_mcp(
    name: str,
    command: tuple[str, ...],
    env_pairs: tuple[str, ...],
    destructive: tuple[str, ...],
    description: str,
    http_url: str | None,
    header_pairs: tuple[str, ...],
) -> None:
    """Register an MCP server as a connector — stdio or HTTP transport.

    Stdio:  pilothouse connectors add-mcp time uvx mcp-server-time -- --tz=UTC
    HTTP:   pilothouse connectors add-mcp time --http https://mcp.example.com/rpc \
                --header "Authorization=Bearer xyz"
    """
    transport = "http" if http_url else "stdio"
    if transport == "stdio" and not command:
        raise click.ClickException("stdio transport requires a command")
    if transport == "http" and command:
        raise click.ClickException("--http and command-args are mutually exclusive")

    argv = list(command)
    env: dict[str, str] = {}
    for p in env_pairs:
        if "=" not in p:
            raise click.ClickException(f"--env expects KEY=VAL, got {p!r}")
        k, _, v = p.partition("=")
        env[k] = v
    headers: dict[str, str] = {}
    for p in header_pairs:
        if "=" not in p:
            raise click.ClickException(f"--header expects NAME=VALUE, got {p!r}")
        k, _, v = p.partition("=")
        headers[k] = v

    async def _go() -> None:
        await init_db()
        spec = McpServerSpec(
            name=name,
            transport=transport,
            command=argv,
            env=env,
            url=http_url or "",
            headers=headers,
            destructive_tools=set(destructive),
            description=description,
        )
        try:
            conn = await register_mcp_server(spec)
        except Exception as exc:
            raise click.ClickException(f"failed to register: {exc}") from exc
        async with session() as s:
            existing = (
                await s.execute(select(McpServer).where(McpServer.name == name))
            ).scalar_one_or_none()
            if existing is None:
                s.add(
                    McpServer(
                        name=name,
                        transport=transport,
                        command=argv,
                        env=env,
                        url=http_url or "",
                        headers=headers,
                        destructive_tools=list(destructive),
                        description=description,
                    )
                )
            else:
                existing.transport = transport
                existing.command = argv
                existing.env = env
                existing.url = http_url or ""
                existing.headers = headers
                existing.destructive_tools = list(destructive)
                existing.description = description
                existing.enabled = True
        console.print(
            f"[green]registered[/] mcp connector {name} ({transport}) with {len(conn.tools())} tool(s)"
        )

    _run(_go())


@connectors.command("remove-mcp")
@click.argument("name")
def connectors_remove_mcp(name: str) -> None:
    """Unregister an MCP server and forget it."""

    async def _go() -> None:
        await init_db()
        removed = await unregister_mcp_server(name)
        async with session() as s:
            existing = (
                await s.execute(select(McpServer).where(McpServer.name == name))
            ).scalar_one_or_none()
            if existing is not None:
                await s.delete(existing)
        if removed:
            console.print(f"[green]removed[/] {name}")
        else:
            console.print(f"[yellow]not registered at runtime[/] (db row cleared if any)")

    _run(_go())


@connectors.command("list")
def connectors_list() -> None:
    """List currently *enabled* connectors (plus their tools)."""

    async def _go() -> None:
        await init_db()
        await get_plugin_manager().load_all()
        table = Table(title="Connectors (enabled)")
        table.add_column("connector")
        table.add_column("live")
        table.add_column("tools")
        for c in conn_registry.connectors.values():
            tool_list = ", ".join(
                (t.name + (" [W]" if t.is_destructive else "")) for t in c.tools()
            )
            table.add_row(c.name, "yes" if c.live else "mock", tool_list)
        console.print(table)
        console.print("[dim]'[W]' = destructive tool (gated by dry_run / approval)[/dim]")

    _run(_go())


# --- agents ---------------------------------------------------------------


@main.group()
def agents() -> None:
    """Manage agents."""


@agents.command("create")
@click.argument("name")
@click.argument("template")
@click.option("--description", default="")
@click.option(
    "--param",
    "params",
    multiple=True,
    help="key=value (repeatable). Values are JSON-parsed when possible.",
)
@click.option("--cron", "schedule_cron", default=None, help="cron expression")
@click.option("--dry-run/--no-dry-run", default=True)
def agents_create(
    name: str,
    template: str,
    description: str,
    params: tuple[str, ...],
    schedule_cron: str | None,
    dry_run: bool,
) -> None:
    if template not in tpl_registry.templates:
        raise click.ClickException(f"unknown template: {template}")

    parsed: dict = {}
    for p in params:
        if "=" not in p:
            raise click.ClickException(f"--param expects k=v, got {p!r}")
        k, _, v = p.partition("=")
        try:
            parsed[k] = json.loads(v)
        except json.JSONDecodeError:
            parsed[k] = v

    async def _go() -> None:
        await init_db()
        async with session() as s:
            existing = (
                await s.execute(select(Agent).where(Agent.name == name))
            ).scalar_one_or_none()
            if existing:
                raise click.ClickException(f"agent name exists: {name}")
            a = Agent(
                name=name,
                template=template,
                description=description,
                params=parsed,
                schedule_cron=schedule_cron,
                dry_run=dry_run,
            )
            s.add(a)
            await s.flush()
            console.print(f"[green]created[/] {a.id}  {a.name}  (template={a.template})")

    _run(_go())


@agents.command("list")
def agents_list() -> None:
    async def _go() -> None:
        await init_db()
        async with session() as s:
            rows = (
                await s.execute(select(Agent).order_by(Agent.created_at.desc()))
            ).scalars().all()
            table = Table(title="Agents")
            for col in ("id", "name", "template", "enabled", "dry_run", "cron"):
                table.add_column(col)
            for a in rows:
                table.add_row(
                    a.id[:8],
                    a.name,
                    a.template,
                    "yes" if a.enabled else "no",
                    "yes" if a.dry_run else "no",
                    a.schedule_cron or "-",
                )
            console.print(table)

    _run(_go())


@agents.command("show")
@click.argument("agent_id")
def agents_show(agent_id: str) -> None:
    async def _go() -> None:
        await init_db()
        async with session() as s:
            a = await _resolve_agent(s, agent_id)
            console.print(f"[bold]{a.name}[/] ({a.id})")
            console.print(f"  template: {a.template}")
            console.print(f"  enabled:  {a.enabled}")
            console.print(f"  dry_run:  {a.dry_run}")
            console.print(f"  cron:     {a.schedule_cron or '-'}")
            console.print(f"  params:")
            console.print(RichJSON(json.dumps(a.params, default=str)))
            runs = (
                await s.execute(
                    select(Run).where(Run.agent_id == a.id).order_by(Run.started_at.desc()).limit(10)
                )
            ).scalars().all()
            t = Table(title="Recent runs")
            for c in ("id", "trigger", "status", "tokens_in", "tokens_out", "started"):
                t.add_column(c)
            for r in runs:
                t.add_row(
                    r.id[:8],
                    r.trigger,
                    r.status.value if hasattr(r.status, "value") else str(r.status),
                    str(r.tokens_input),
                    str(r.tokens_output),
                    r.started_at.strftime("%Y-%m-%d %H:%M:%S"),
                )
            console.print(t)

    _run(_go())


@agents.command("trigger")
@click.argument("agent_id")
@click.option("--file", "payload_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--dry-run/--no-dry-run", default=None)
def agents_trigger(agent_id: str, payload_file: str | None, dry_run: bool | None) -> None:
    """Manually trigger an agent. Reads JSON payload from --file or stdin."""
    raw = ""
    if payload_file:
        raw = Path(payload_file).read_text()
    elif not sys.stdin.isatty():
        raw = sys.stdin.read()
    payload: dict = {}
    if raw.strip():
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise click.ClickException(f"invalid JSON payload: {exc}") from exc

    async def _go() -> None:
        await init_db()
        async with session() as s:
            a = await _resolve_agent(s, agent_id)
            aid = a.id
        run_id = await execute_agent(
            agent_id=aid,
            trigger="manual",
            trigger_payload=payload,
            dry_run_override=dry_run,
        )
        console.print(f"[green]triggered[/] run {run_id}")
        await _show_run(run_id)

    _run(_go())


@agents.command("delete")
@click.argument("agent_id")
def agents_delete(agent_id: str) -> None:
    async def _go() -> None:
        await init_db()
        async with session() as s:
            a = await _resolve_agent(s, agent_id)
            await s.delete(a)
            console.print(f"[green]deleted[/] {a.name}")

    _run(_go())


# --- runs -----------------------------------------------------------------


@main.group()
def runs() -> None:
    """Inspect runs."""


@runs.command("list")
@click.option("--limit", default=20, type=int, help="max rows to return (cap 500)")
@click.option("--offset", default=0, type=int, help="skip the first N rows")
@click.option("--status", default=None, help="exact RunStatus (succeeded/failed/running/awaiting_approval/cancelled)")
@click.option("--agent", default=None, help="exact agent name")
@click.option("--trigger", default=None, help="substring match against Run.trigger (e.g. 'webhook')")
@click.option("-q", "summary_q", default=None, help="substring match against Run.summary")
def runs_list(
    limit: int,
    offset: int,
    status: str | None,
    agent: str | None,
    trigger: str | None,
    summary_q: str | None,
) -> None:
    """List recent runs across all agents. Mirrors the /runs HTTP endpoint."""

    async def _go() -> None:
        await init_db()
        async with session() as s:
            stmt = (
                select(Run, Agent.name)
                .join(Agent, Agent.id == Run.agent_id)
                .order_by(Run.started_at.desc())
                .limit(max(1, min(500, limit)))
                .offset(max(0, offset))
            )
            if status:
                try:
                    stmt = stmt.where(Run.status == RunStatus(status))
                except ValueError as exc:
                    raise click.ClickException(f"unknown status: {status}") from exc
            if trigger:
                stmt = stmt.where(Run.trigger.like(f"%{trigger}%"))
            if summary_q:
                stmt = stmt.where(Run.summary.like(f"%{summary_q}%"))
            if agent:
                stmt = stmt.where(Agent.name == agent)
            rows = (await s.execute(stmt)).all()

            table = Table(title="Runs")
            for col in ("id", "agent", "trigger", "status", "tok in/out", "started", "summary"):
                table.add_column(col)
            for r, agent_name in rows:
                summary = (r.summary or "").splitlines()[0] if r.summary else ""
                if len(summary) > 60:
                    summary = summary[:57] + "…"
                table.add_row(
                    r.id[:8],
                    agent_name,
                    r.trigger,
                    r.status.value if hasattr(r.status, "value") else str(r.status),
                    f"{r.tokens_input}/{r.tokens_output}",
                    r.started_at.strftime("%Y-%m-%d %H:%M:%S"),
                    summary,
                )
            console.print(table)
            if not rows:
                console.print("[dim]no runs match these filters[/]")

    _run(_go())


@runs.command("show")
@click.argument("run_id")
def runs_show(run_id: str) -> None:
    _run(_show_run(run_id))


@runs.command("logs")
@click.argument("run_id")
@click.option("--tail", default=0, type=int, help="show only the last N events")
@click.option(
    "--kind",
    "kind_filter",
    multiple=True,
    help="filter to specific event kinds (repeatable)",
)
def runs_logs(run_id: str, tail: int, kind_filter: tuple[str, ...]) -> None:
    """Pretty-printed event timeline for one run.

    Designed for incident review: each event line shows time, kind, and
    a one-line summary of the most useful field (tool name, model
    iteration, etc.). For the full event payload pass the run id to
    `runs show` or download `/runs/{id}/export.json`.
    """
    from rich.text import Text

    KIND_STYLE = {
        "run_started": "bold green",
        "run_finished": "bold green",
        "run_terminal": "bold green",
        "run_cancelled": "bold yellow",
        "approval_requested": "bold yellow",
        "approval_resolved": "green",
        "approval_expired": "yellow",
        "tool_call": "cyan",
        "tool_result": "cyan",
        "model_turn": "blue",
        "decision": "dim",
        "error": "bold red",
    }

    async def _go() -> None:
        await init_db()
        async with session() as s:
            r = (
                await s.execute(select(Run).where(Run.id.like(run_id + "%")))
            ).scalar_one_or_none()
            if r is None:
                raise click.ClickException(f"run not found: {run_id}")
            events = (
                await s.execute(
                    select(Event).where(Event.run_id == r.id).order_by(Event.created_at)
                )
            ).scalars().all()
        if kind_filter:
            wanted = set(kind_filter)
            events = [
                e for e in events
                if (e.kind.value if hasattr(e.kind, "value") else str(e.kind)) in wanted
            ]
        if tail > 0:
            events = events[-tail:]

        console.rule(f"Run {r.id[:8]}  status={r.status.value if hasattr(r.status, 'value') else r.status}")
        for e in events:
            kind = e.kind.value if hasattr(e.kind, "value") else str(e.kind)
            ts = e.created_at.strftime("%H:%M:%S")
            style = KIND_STYLE.get(kind, "white")
            summary = _summarize_event(kind, e.data)
            line = Text()
            line.append(f"{ts}  ", style="dim")
            line.append(f"{kind:<22} ", style=style)
            line.append(summary)
            console.print(line)

    _run(_go())


def _summarize_event(kind: str, data: dict) -> str:
    """One-line summary of an event payload — chosen field per kind."""
    if kind == "tool_call":
        return f"{data.get('tool', '?')}({_short_json(data.get('input'))})"
    if kind == "tool_result":
        return f"{data.get('tool', '?')} → {'ERR' if data.get('is_error') else 'ok'}"
    if kind == "model_turn":
        usage = data.get("usage") or {}
        return (
            f"iter {data.get('iteration', '?')}  "
            f"stop={data.get('stop_reason', '?')}  "
            f"tok={usage.get('input_tokens', 0)}/{usage.get('output_tokens', 0)}"
        )
    if kind == "approval_requested":
        return f"{data.get('tool', '?')}  (approval={data.get('approval_id', '?')[:8]})"
    if kind == "approval_resolved":
        return f"{data.get('decision', '?')} by {data.get('by', '?')}"
    if kind == "approval_expired":
        return f"{data.get('tool', '?')}  ttl={data.get('ttl_minutes', '?')}m"
    if kind == "run_terminal":
        return f"status={data.get('status', '?')}  tok={data.get('tokens_input', 0)}/{data.get('tokens_output', 0)}"
    if kind == "run_started":
        return f"dry_run={data.get('dry_run')}  tools={len(data.get('tool_names') or [])}"
    if kind == "run_cancelled":
        return f"by {data.get('by', '?')}"
    if kind == "error":
        return f"{data.get('phase', '?')}: {data.get('error', '?')[:120]}"
    if kind == "decision":
        return data.get("label", "")
    return _short_json(data)


def _short_json(obj, limit: int = 80) -> str:
    if obj is None:
        return ""
    text = json.dumps(obj, default=str, separators=(",", ":"))
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


@runs.command("retry")
@click.argument("run_id")
@click.option("--dry-run/--no-dry-run", default=None)
def runs_retry(run_id: str, dry_run: bool | None) -> None:
    """Re-execute a run with the same trigger payload."""

    async def _go() -> None:
        await init_db()
        async with session() as s:
            r = (
                await s.execute(select(Run).where(Run.id.like(run_id + "%")))
            ).scalar_one_or_none()
            if r is None:
                raise click.ClickException(f"run not found: {run_id}")
            rid = r.id
        new_id = await retry_run(rid, dry_run_override=dry_run)
        console.print(f"[green]retried[/] new run {new_id}")
        await _show_run(new_id)

    _run(_go())


@runs.command("cancel")
@click.argument("run_id")
@click.option("--by", default="operator", help="who cancelled")
def runs_cancel(run_id: str, by: str) -> None:
    async def _go() -> None:
        await init_db()
        async with session() as s:
            r = (
                await s.execute(select(Run).where(Run.id.like(run_id + "%")))
            ).scalar_one_or_none()
            if r is None:
                raise click.ClickException(f"run not found: {run_id}")
            rid = r.id
        await cancel_run(rid, by=by)
        console.print(f"[yellow]cancel requested[/] {rid[:8]}")
        await _show_run(rid)

    _run(_go())


@main.command("sweep-approvals")
def sweep_approvals_cmd() -> None:
    """One-shot expiry sweep — useful from cron when not running the HTTP server."""

    async def _go() -> None:
        await init_db()
        n = await sweep_expired_approvals()
        console.print(f"[green]expired[/] {n} approval(s)")

    _run(_go())


async def _show_run(run_id: str) -> None:
    await init_db()
    async with session() as s:
        r = (await s.execute(select(Run).where(Run.id.like(run_id + "%")))).scalar_one_or_none()
        if r is None:
            raise click.ClickException(f"run not found: {run_id}")
        console.rule(f"Run {r.id[:8]}  status={r.status.value if hasattr(r.status, 'value') else r.status}")
        console.print(f"trigger:  {r.trigger}")
        console.print(f"tokens:   in={r.tokens_input}  out={r.tokens_output}")
        console.print(f"cost:     ${r.cost_usd_cents / 10000:.4f}")
        console.print("\n[bold]Summary[/]")
        console.print(r.summary or "(empty)")
        console.print("\n[bold]Events[/]")
        events = (
            await s.execute(select(Event).where(Event.run_id == r.id).order_by(Event.created_at))
        ).scalars().all()
        for ev in events:
            kind = ev.kind.value if hasattr(ev.kind, "value") else str(ev.kind)
            console.print(f"[dim]{ev.created_at.strftime('%H:%M:%S')}[/] [cyan]{kind}[/]")
            console.print(RichJSON(json.dumps(ev.data, default=str)))


async def _resolve_agent(s, agent_id: str) -> Agent:
    """Accept full id, id prefix, or name."""
    candidate = (
        await s.execute(select(Agent).where(Agent.id == agent_id))
    ).scalar_one_or_none()
    if candidate:
        return candidate
    candidate = (
        await s.execute(select(Agent).where(Agent.name == agent_id))
    ).scalar_one_or_none()
    if candidate:
        return candidate
    candidate = (
        await s.execute(select(Agent).where(Agent.id.like(agent_id + "%")))
    ).scalar_one_or_none()
    if candidate:
        return candidate
    raise click.ClickException(f"agent not found: {agent_id}")


# --- plugins --------------------------------------------------------------


@main.group()
def plugins() -> None:
    """List and toggle plugins (templates, connectors, notifiers, …)."""


@plugins.command("list")
def plugins_list() -> None:
    """Discover plugins (rescan every source) and show their state."""

    async def _go() -> None:
        await init_db()
        mgr = get_plugin_manager()
        await mgr.load_all()
        rows = mgr.list_plugins()
        table = Table(title=f"Plugins ({len(rows)})")
        for col in ("name", "kinds", "version", "enabled", "source", "description"):
            table.add_column(col)
        for r in rows:
            table.add_row(
                r["name"],
                ",".join(r["kinds"]),
                r["version"] or "-",
                "[green]on[/]" if r["enabled"] else "[dim]off[/]",
                r["source"],
                r["description"][:60],
            )
        console.print(table)

    _run(_go())


@plugins.command("enable")
@click.argument("name")
def plugins_enable(name: str) -> None:
    async def _go() -> None:
        await init_db()
        mgr = get_plugin_manager()
        await mgr.load_all()
        try:
            await mgr.enable(name)
        except KeyError as exc:
            raise click.ClickException(str(exc)) from exc
        console.print(f"[green]enabled[/] {name}")

    _run(_go())


@plugins.command("disable")
@click.argument("name")
def plugins_disable(name: str) -> None:
    async def _go() -> None:
        await init_db()
        mgr = get_plugin_manager()
        await mgr.load_all()
        try:
            await mgr.disable(name)
        except KeyError as exc:
            raise click.ClickException(str(exc)) from exc
        console.print(f"[yellow]disabled[/] {name}")

    _run(_go())


@plugins.command("reload")
def plugins_reload() -> None:
    """Re-discover from all sources. Useful after dropping a new .py
    into the plugin directory or installing a new plugin package."""

    async def _go() -> None:
        await init_db()
        mgr = get_plugin_manager()
        await mgr.load_all()
        console.print(f"[green]reloaded[/] — {len(mgr.list_plugins())} plugin(s) registered")

    _run(_go())


@plugins.command("doctor")
def plugins_doctor() -> None:
    """Show plugins that are enabled but cannot run due to bad config.

    Exits non-zero if any plugin is misconfigured — useful in CI as a
    pre-deploy check that all required env vars / operator values are set.
    """

    async def _go() -> None:
        await init_db()
        mgr = get_plugin_manager()
        await mgr.load_all()
        bad = mgr.doctor()
        if not bad:
            console.print("[green]ok[/] — every enabled plugin is configured")
            return
        table = Table(title="Misconfigured plugins")
        table.add_column("name")
        table.add_column("reason", style="red")
        for entry in bad:
            table.add_row(entry["name"], entry["reason"])
        console.print(table)
        raise click.exceptions.Exit(code=1)

    _run(_go())


@plugins.group("config")
def plugins_config() -> None:
    """Get / set per-plugin configuration values."""


@plugins_config.command("show")
@click.argument("name")
@click.option("--reveal", is_flag=True, help="Show secret values in plaintext")
def plugins_config_show(name: str, reveal: bool) -> None:
    async def _go() -> None:
        await init_db()
        mgr = get_plugin_manager()
        await mgr.load_all()
        try:
            cfg = await mgr.get_config(name, mask_secrets=not reveal)
        except KeyError as exc:
            raise click.ClickException(str(exc)) from exc
        if not cfg:
            console.print(f"[dim]plugin {name} declares no config schema[/]")
            return
        table = Table(title=f"Config for {name}")
        for col in ("field", "value", "source", "required", "secret"):
            table.add_column(col)
        for k, v in cfg.items():
            table.add_row(
                k,
                v["value"] or "[dim](unset)[/]",
                v["source"] or "-",
                "yes" if v["required"] else "",
                "yes" if v["secret"] else "",
            )
        console.print(table)

    _run(_go())


@plugins_config.command("set")
@click.argument("name")
@click.argument("key")
@click.argument("value")
def plugins_config_set(name: str, key: str, value: str) -> None:
    """Set a config value. Re-activates the plugin in-place."""

    async def _go() -> None:
        await init_db()
        mgr = get_plugin_manager()
        await mgr.load_all()
        try:
            await mgr.set_config(name, key, value)
        except KeyError as exc:
            raise click.ClickException(str(exc)) from exc
        console.print(f"[green]set[/] {name}.{key}")
        bad = mgr.doctor()
        if any(b["name"] == name for b in bad):
            console.print(
                f"[yellow]warning[/]: {name} still misconfigured — see "
                "[bold]plugins doctor[/]"
            )

    _run(_go())


@plugins_config.command("unset")
@click.argument("name")
@click.argument("key")
def plugins_config_unset(name: str, key: str) -> None:
    async def _go() -> None:
        await init_db()
        mgr = get_plugin_manager()
        await mgr.load_all()
        try:
            await mgr.unset_config(name, key)
        except KeyError as exc:
            raise click.ClickException(str(exc)) from exc
        console.print(f"[green]unset[/] {name}.{key}")

    _run(_go())


@plugins.command("info")
@click.argument("name")
@click.option("--reveal", is_flag=True, help="Show secret config values in plaintext")
def plugins_info(name: str, reveal: bool) -> None:
    """Detailed view of a single plugin: meta, schema, config, status."""

    async def _go() -> None:
        await init_db()
        mgr = get_plugin_manager()
        await mgr.load_all()
        rows = {p["name"]: p for p in mgr.list_plugins()}
        if name not in rows:
            raise click.ClickException(f"unknown plugin: {name}")
        p = rows[name]

        console.rule(f"[bold]{p['name']}[/]  v{p['version']}")
        console.print(f"  description : {p['description']}")
        console.print(f"  kinds       : {', '.join(p['kinds']) or '-'}")
        console.print(f"  source      : {p['source']}")
        if p["enabled"] and not p["misconfig_reason"]:
            status_str = "[green]on[/]"
        elif p["enabled"]:
            status_str = "[red]misconfigured[/]"
        else:
            status_str = "[dim]off[/]"
        console.print(f"  status      : {status_str}")
        if p["misconfig_reason"]:
            console.print(f"  reason      : [red]{p['misconfig_reason']}[/]")
        console.print(f"  compat      : {p['compat']}")

        if not p["config_schema"]:
            console.print("\n[dim]No config schema declared.[/]")
            return

        console.print("\n[bold]Config[/]")
        cfg = await mgr.get_config(name, mask_secrets=not reveal)
        table = Table(show_header=True)
        for col in ("field", "value", "source", "required", "secret", "description"):
            table.add_column(col)
        for f in p["config_schema"]:
            v = cfg.get(f["name"], {"value": "", "source": ""})
            table.add_row(
                f["name"],
                v["value"] or "[dim](unset)[/]",
                v["source"] or "-",
                "yes" if f["required"] else "",
                "yes" if f["secret"] else "",
                (f["description"] or "")[:50],
            )
        console.print(table)

    _run(_go())


@plugins.command("scaffold")
@click.argument("kind", type=click.Choice(["notifier", "connector", "template", "trigger", "hook"]))
@click.argument("name")
@click.option(
    "-d", "--dir", "out_dir",
    default=None,
    help="Where to write the files. Defaults to PILOTHOUSE_PLUGIN_DIR or ./plugins/.",
)
@click.option("--force", is_flag=True, help="Overwrite existing files")
def plugins_scaffold(kind: str, name: str, out_dir: str | None, force: bool) -> None:
    """Generate a starter plugin file (and unit-test file).

    Example:
        pilothouse plugins scaffold notifier my_discord
        pilothouse plugins reload
        pilothouse plugins config set my_discord target https://...
    """
    import os
    from pathlib import Path

    from .plugins.scaffold import render

    try:
        scaffold = render(kind, name)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    target = Path(
        out_dir or os.getenv("PILOTHOUSE_PLUGIN_DIR") or "plugins"
    )
    target.mkdir(parents=True, exist_ok=True)
    test_dir = target / "tests"
    test_dir.mkdir(parents=True, exist_ok=True)

    plugin_path = target / scaffold.plugin_filename
    test_path = test_dir / scaffold.test_filename

    if (plugin_path.exists() or test_path.exists()) and not force:
        raise click.ClickException(
            f"refusing to overwrite existing files at {plugin_path} or {test_path}; "
            "pass --force to overwrite"
        )
    plugin_path.write_text(scaffold.plugin_body)
    test_path.write_text(scaffold.test_body)
    # Drop a tiny pytest.ini so the scaffolded async tests run with
    # `pytest tests/` out of the box. Idempotent.
    ini = target / "pytest.ini"
    if not ini.exists():
        ini.write_text(_SCAFFOLD_PYTEST_INI)
        console.print(f"[green]wrote[/] {ini}")

    console.print(f"[green]wrote[/] {plugin_path}")
    console.print(f"[green]wrote[/] {test_path}")
    console.print(
        f"\n[dim]Next:[/]\n"
        f"  PILOTHOUSE_PLUGIN_DIR={target} pilothouse plugins reload\n"
        f"  PILOTHOUSE_PLUGIN_DIR={target} pilothouse plugins info {name}\n"
        f"  pytest {test_path}    # requires pytest-asyncio\n"
    )


@plugins.command("install")
@click.argument("package")
@click.option("--upgrade", is_flag=True, help="Pass --upgrade to pip")
@click.option("--pre", is_flag=True, help="Pass --pre to pip")
def plugins_install(package: str, upgrade: bool, pre: bool) -> None:
    """Pip-install a plugin distribution + reload to discover its entry points.

    Thin wrapper around `pip install`. Use the same package spec you'd
    pass to pip:
        pilothouse plugins install pilothouse-discord
        pilothouse plugins install ./my-plugin                  # local sdist
        pilothouse plugins install 'git+https://github.com/...'  # vcs
    """
    import subprocess
    import sys

    cmd = [sys.executable, "-m", "pip", "install"]
    if upgrade:
        cmd.append("--upgrade")
    if pre:
        cmd.append("--pre")
    cmd.append(package)
    console.print(f"[dim]$ {' '.join(cmd)}[/]")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.stdout.strip():
        console.print(proc.stdout)
    if proc.returncode != 0:
        if proc.stderr.strip():
            console.print(f"[red]{proc.stderr}[/]")
        raise click.ClickException(f"pip install failed (exit {proc.returncode})")

    async def _go() -> None:
        await init_db()
        mgr = get_plugin_manager()
        before = {p["name"] for p in mgr.list_plugins()}
        await mgr.load_all()
        after = {p["name"] for p in mgr.list_plugins()}
        new = sorted(after - before)
        if new:
            console.print(f"[green]registered[/] new plugins: {', '.join(new)}")
        else:
            console.print(
                "[yellow]no new plugins discovered[/] — does the package declare a "
                "[bold]pilothouse.plugins[/] entry point?"
            )

    _run(_go())


# --- tenants --------------------------------------------------------------


@main.group()
def tenants() -> None:
    """Manage tenants and their API keys."""


@tenants.command("list")
def tenants_list() -> None:
    async def _go() -> None:
        await init_db()
        rows = await list_tenants()
        table = Table(title="Tenants")
        for col in ("id", "name", "display_name", "api_keys", "created"):
            table.add_column(col)
        for t in rows:
            table.add_row(
                t.id[:8],
                t.name,
                t.display_name or "",
                str(len(t.api_keys or [])),
                t.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            )
        console.print(table)

    _run(_go())


@tenants.command("create")
@click.argument("name")
@click.option("--display-name", default="")
def tenants_create(name: str, display_name: str) -> None:
    async def _go() -> None:
        await init_db()
        try:
            t = await create_tenant(name, display_name)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        console.print(f"[green]created[/] tenant {name} ({t.id})")

    _run(_go())


@tenants.command("add-key")
@click.argument("tenant_name")
@click.option("--key", default=None, help="key to add (default: generate one)")
def tenants_add_key(tenant_name: str, key: str | None) -> None:
    """Add an API key to a tenant. If --key is omitted, a random one is generated."""
    import secrets

    async def _go() -> None:
        await init_db()
        new_key = key or ("phk_" + secrets.token_urlsafe(24))
        try:
            await add_api_key(tenant_name, new_key)
        except KeyError as exc:
            raise click.ClickException(str(exc)) from exc
        console.print(f"[green]added key[/] for tenant {tenant_name}: [bold]{new_key}[/]")
        console.print("[dim](save it now — Pilothouse never displays it again in plain form)[/]")

    _run(_go())


@tenants.command("remove-key")
@click.argument("tenant_name")
@click.argument("key")
def tenants_remove_key(tenant_name: str, key: str) -> None:
    async def _go() -> None:
        await init_db()
        try:
            await remove_api_key(tenant_name, key)
        except KeyError as exc:
            raise click.ClickException(str(exc)) from exc
        console.print(f"[green]removed[/] key for tenant {tenant_name}")

    _run(_go())


@tenants.command("set-quota")
@click.argument("tenant_name")
@click.option("--max-agents", type=int, default=None, help="0 = unlimited")
@click.option("--max-runs-per-day", type=int, default=None, help="0 = unlimited")
def tenants_set_quota(
    tenant_name: str, max_agents: int | None, max_runs_per_day: int | None
) -> None:
    """Set per-tenant quotas. Either flag may be omitted to leave unchanged."""
    if max_agents is None and max_runs_per_day is None:
        raise click.ClickException("pass at least one of --max-agents / --max-runs-per-day")

    async def _go() -> None:
        await init_db()
        try:
            t = await set_quota(
                tenant_name,
                max_agents=max_agents,
                max_runs_per_day=max_runs_per_day,
            )
        except KeyError as exc:
            raise click.ClickException(str(exc)) from exc
        console.print(
            f"[green]updated[/] {tenant_name}: max_agents={t.max_agents} "
            f"max_runs_per_day={t.max_runs_per_day}"
        )

    _run(_go())


@tenants.command("show-keys")
@click.argument("tenant_name")
def tenants_show_keys(tenant_name: str) -> None:
    """List API keys for a tenant — masked, never plaintext.

    Operators administering tenants need to know *which* keys are
    active (count, fingerprint) without the CLI itself becoming a
    credential leak. We show first 4 + last 4 chars only.
    """
    async def _go() -> None:
        await init_db()
        async with session() as s:
            from .models import Tenant

            t = (
                await s.execute(select(Tenant).where(Tenant.name == tenant_name))
            ).scalar_one_or_none()
            if t is None:
                raise click.ClickException(f"unknown tenant: {tenant_name}")
        keys = list(t.api_keys or [])
        if not keys:
            console.print(f"[yellow]no keys[/] for tenant {tenant_name}")
            return
        table = Table(title=f"Keys for {tenant_name}")
        table.add_column("idx")
        table.add_column("masked")
        table.add_column("length")
        for i, k in enumerate(keys):
            mask = k[:4] + "…" + k[-4:] if len(k) > 10 else "…" * len(k)
            table.add_row(str(i), mask, str(len(k)))
        console.print(table)

    _run(_go())


@tenants.command("delete")
@click.argument("name")
@click.confirmation_option(prompt="Delete this tenant and ALL its agents/runs/approvals?")
def tenants_delete(name: str) -> None:
    async def _go() -> None:
        await init_db()
        try:
            ok = await delete_tenant(name)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        if ok:
            console.print(f"[green]deleted[/] tenant {name}")
        else:
            console.print(f"[yellow]not found[/] {name}")

    _run(_go())


# --- approvals ------------------------------------------------------------


@main.group()
def approvals() -> None:
    """List and resolve pending approvals."""


@approvals.command("list")
@click.option("--status", default="pending", help="pending|approved|rejected|all")
def approvals_list(status: str) -> None:
    async def _go() -> None:
        await init_db()
        async with session() as s:
            q = select(Approval).order_by(Approval.created_at.desc())
            if status != "all":
                try:
                    q = q.where(Approval.status == ApprovalStatus(status))
                except ValueError:
                    raise click.ClickException(f"unknown status: {status}")
            rows = (await s.execute(q)).scalars().all()
            table = Table(title=f"Approvals ({status})")
            for col in ("id", "run", "tool", "status", "created"):
                table.add_column(col)
            for a in rows:
                table.add_row(
                    a.id[:8],
                    a.run_id[:8],
                    a.tool_name,
                    a.status.value if hasattr(a.status, "value") else str(a.status),
                    a.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                )
            console.print(table)

    _run(_go())


@approvals.command("show")
@click.argument("approval_id")
def approvals_show(approval_id: str) -> None:
    async def _go() -> None:
        await init_db()
        async with session() as s:
            a = (
                await s.execute(select(Approval).where(Approval.id.like(approval_id + "%")))
            ).scalar_one_or_none()
            if a is None:
                raise click.ClickException(f"approval not found: {approval_id}")
            console.rule(f"Approval {a.id[:8]}")
            console.print(f"run:        {a.run_id[:8]}")
            console.print(f"tool:       {a.tool_name}")
            console.print(f"status:     {a.status.value if hasattr(a.status, 'value') else a.status}")
            console.print(f"created:    {a.created_at}")
            if a.resolved_at:
                console.print(f"resolved:   {a.resolved_at} by {a.resolved_by}")
            console.print("[bold]Tool input[/]")
            console.print(RichJSON(json.dumps(a.tool_input, default=str)))
            console.print("[bold]Rationale[/]")
            console.print(a.rationale or "(none)")

    _run(_go())


@approvals.command("approve")
@click.argument("approval_id")
@click.option("--by", default="operator", help="who approved")
def approvals_approve(approval_id: str, by: str) -> None:
    _run(_resolve_approval(approval_id, approve=True, by=by, reason=""))


@approvals.command("reject")
@click.argument("approval_id")
@click.option("--by", default="operator", help="who rejected")
@click.option("--reason", default="", help="rejection reason")
def approvals_reject(approval_id: str, by: str, reason: str) -> None:
    _run(_resolve_approval(approval_id, approve=False, by=by, reason=reason))


@approvals.command("approve-all")
@click.option("--tool", default=None, help="only approvals for this tool name")
@click.option("--agent", default=None, help="only approvals from this agent name")
@click.option("--by", default="operator")
@click.confirmation_option(prompt="Approve every matching pending approval and resume?")
def approvals_approve_all(tool: str | None, agent: str | None, by: str) -> None:
    """Bulk-approve every pending approval matching the filters and auto-resume their runs."""
    _run(_bulk_resolve(decision="approve", by=by, tool=tool, agent=agent, reason=""))


@approvals.command("reject-all")
@click.option("--tool", default=None, help="only approvals for this tool name")
@click.option("--agent", default=None, help="only approvals from this agent name")
@click.option("--by", default="operator")
@click.option("--reason", default="bulk reject", help="rejection reason recorded on each")
@click.confirmation_option(prompt="Reject every matching pending approval?")
def approvals_reject_all(tool: str | None, agent: str | None, by: str, reason: str) -> None:
    _run(_bulk_resolve(decision="reject", by=by, tool=tool, agent=agent, reason=reason))


async def _bulk_resolve(*, decision: str, by: str, tool: str | None, agent: str | None, reason: str) -> None:
    from datetime import datetime, timezone

    await init_db()
    tid = await ensure_default_tenant()
    affected_runs: set[str] = set()
    count = 0
    async with session() as s:
        q = select(Approval).where(
            Approval.tenant_id == tid, Approval.status == ApprovalStatus.pending
        )
        if tool:
            q = q.where(Approval.tool_name == tool)
        if agent:
            q = (
                q.join(Run, Run.id == Approval.run_id)
                .join(Agent, Agent.id == Run.agent_id)
                .where(Agent.name == agent)
            )
        rows = (await s.execute(q)).scalars().all()
        approved = decision == "approve"
        for a in rows:
            a.status = ApprovalStatus.approved if approved else ApprovalStatus.rejected
            a.resolved_by = by
            a.rejection_reason = "" if approved else reason
            a.resolved_at = datetime.now(timezone.utc)
            affected_runs.add(a.run_id)
            count += 1
    console.print(f"[green]resolved[/] {count} approval(s)")
    for rid in affected_runs:
        async with session() as s:
            remaining = (
                await s.execute(
                    select(Approval).where(
                        Approval.run_id == rid, Approval.status == ApprovalStatus.pending
                    )
                )
            ).scalars().first()
        if remaining is None:
            try:
                await resume_run(rid)
            except Exception as exc:  # pragma: no cover — log only
                console.print(f"[red]resume failed for {rid[:8]}: {exc}[/]")
    if affected_runs:
        console.print(f"[green]resumed[/] {len(affected_runs)} run(s) where all approvals are now resolved")


async def _resolve_approval(approval_id: str, *, approve: bool, by: str, reason: str) -> None:
    await init_db()
    run_id: str | None = None
    async with session() as s:
        a = (
            await s.execute(select(Approval).where(Approval.id.like(approval_id + "%")))
        ).scalar_one_or_none()
        if a is None:
            raise click.ClickException(f"approval not found: {approval_id}")
        if a.status != ApprovalStatus.pending:
            raise click.ClickException(f"approval already {a.status.value}")
        from datetime import datetime, timezone

        a.status = ApprovalStatus.approved if approve else ApprovalStatus.rejected
        a.resolved_by = by
        a.rejection_reason = "" if approve else reason
        a.resolved_at = datetime.now(timezone.utc)
        run_id = a.run_id

        remaining = (
            await s.execute(
                select(Approval).where(
                    Approval.run_id == run_id, Approval.status == ApprovalStatus.pending
                )
            )
        ).scalars().first()
        all_done = remaining is None
    console.print(f"[green]{'approved' if approve else 'rejected'}[/] {a.id[:8]}")
    if all_done and run_id is not None:
        console.print(f"all approvals for run {run_id[:8]} resolved — resuming")
        await resume_run(run_id)
        await _show_run(run_id)


# --- declarative apply ----------------------------------------------------


@main.command()
@click.option("-f", "--file", "manifest_path", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--auto-approve", is_flag=True, help="Skip the confirmation prompt")
def apply(manifest_path: str, auto_approve: bool) -> None:
    """Apply an agents manifest (YAML or JSON) to the running deployment.

    Computes a diff between the manifest and the current DB state, prints
    the plan, and (unless --auto-approve) prompts before persisting.
    """

    async def _go() -> None:
        await init_db()
        manifest = load_manifest(manifest_path)
        plan = await compute_plan(manifest)
        console.print(render_plan(plan))
        if not plan.changed:
            return
        if not auto_approve:
            if not click.confirm("\nApply this plan?", default=False):
                console.print("[yellow]aborted[/]")
                return
        await apply_plan(manifest, plan)
        console.print("[green]applied[/]")

    _run(_go())


@main.command()
@click.option("-f", "--file", "manifest_path", required=True, type=click.Path(exists=True, dir_okay=False))
def plan(manifest_path: str) -> None:
    """Show what `apply` would do — no side effects."""

    async def _go() -> None:
        await init_db()
        manifest = load_manifest(manifest_path)
        plan = await compute_plan(manifest)
        console.print(render_plan(plan))

    _run(_go())


@main.command()
@click.option("-o", "--output", default="-", help="output file, '-' for stdout")
@click.option("--prune", is_flag=True, help="emit prune=true so missing agents are removed on next apply")
def export(output: str, prune: bool) -> None:
    """Dump the current agents as a manifest, ready for git."""
    import sys

    import yaml

    async def _go() -> None:
        await init_db()
        async with session() as s:
            rows = (await s.execute(select(Agent).order_by(Agent.name))).scalars().all()
        manifest: dict[str, Any] = {
            "version": 1,
            "prune": prune,
            "agents": [
                {
                    "name": a.name,
                    "template": a.template,
                    "description": a.description,
                    "params": a.params or {},
                    "schedule_cron": a.schedule_cron,
                    "enabled": a.enabled,
                    "dry_run": a.dry_run,
                }
                for a in rows
            ],
        }
        text = yaml.safe_dump(manifest, sort_keys=False)
        if output == "-":
            sys.stdout.write(text)
        else:
            from pathlib import Path

            Path(output).write_text(text)
            console.print(f"[green]wrote[/] {output} ({len(rows)} agent(s))")

    _run(_go())


# --- temporal -------------------------------------------------------------


@main.group()
def temporal() -> None:
    """Inspect / manage Temporal executor mode."""


@temporal.command("status")
def temporal_status() -> None:
    """Show whether Temporal is active and how it's configured."""
    from .config import get_settings
    from .orchestration.executor import executor_kind

    s = get_settings()
    addr = (s.temporal_address or "").strip()
    if not addr:
        console.print("[green]inprocess[/] mode — Temporal not configured.")
        console.print("  set PILOTHOUSE_TEMPORAL_ADDRESS to enable workflow durability.")
        return
    try:
        kind = executor_kind()
        console.print(f"[green]temporal[/] mode — executor: {kind}")
    except RuntimeError as exc:
        console.print(f"[red]error[/]: {exc}")
        return
    console.print(f"  address    : {addr}")
    console.print(f"  namespace  : {s.temporal_namespace}")
    console.print(f"  task_queue : {s.temporal_task_queue}")
    if addr == "dev":
        console.print(
            "  note       : dev mode = in-process Temporal server. "
            "Workflows are durable but the server dies with this process."
        )


# --- demo -----------------------------------------------------------------


@main.command()
def demo() -> None:
    """Bootstrap one of each agent and run them in mock mode.

    Idempotent: re-running uses existing agents instead of creating duplicates.
    """

    async def _go() -> None:
        await init_db()
        async with session() as s:
            wanted = [
                ("triage-demo", "datadog_alert_triage", {"service": "checkout", "slack_channel": "#oncall"}),
                ("scanner-demo", "pr_security_scanner", {"repo": "acme/api", "auto_comment": True}),
                ("k8s-demo", "k8s_pod_investigator", {"service": "checkout"}),
            ]
            agents_ids: list[str] = []
            for name, tpl, params in wanted:
                existing = (
                    await s.execute(select(Agent).where(Agent.name == name))
                ).scalar_one_or_none()
                if existing:
                    agents_ids.append(existing.id)
                    continue
                a = Agent(name=name, template=tpl, params=params, dry_run=True)
                s.add(a)
                await s.flush()
                agents_ids.append(a.id)

        sample_payloads = [
            {"alert_id": "12345", "service": "checkout"},
            {"pull_request": {"number": 4711}, "repository": {"full_name": "acme/api"}},
            {"commonLabels": {"pod": "checkout-7d8c-xyz", "namespace": "prod", "service": "checkout"}},
        ]
        for aid, payload in zip(agents_ids, sample_payloads):
            console.rule(f"triggering {aid[:8]}")
            run_id = await execute_agent(
                agent_id=aid, trigger="manual", trigger_payload=payload
            )
            await _show_run(run_id)

    _run(_go())


if __name__ == "__main__":
    main()
