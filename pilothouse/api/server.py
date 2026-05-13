"""FastAPI app — management API + webhook receivers.

Endpoints:

  Management
    GET    /healthz
    GET    /templates                 — list registered templates
    GET    /connectors                — list connectors + live status
    POST   /agents                    — create an agent
    GET    /agents                    — list agents
    GET    /agents/{id}               — get one
    PATCH  /agents/{id}               — update
    DELETE /agents/{id}               — delete
    POST   /agents/{id}/trigger       — manually trigger a run
    GET    /agents/{id}/runs          — recent runs for an agent
    GET    /runs/{id}                 — run details
    GET    /runs/{id}/events          — audit log for a run

  Webhooks  (no auth; HMAC if PILOTHOUSE_WEBHOOK_SECRET is set)
    POST   /webhooks/datadog/{agent_id}
    POST   /webhooks/github/{agent_id}
    POST   /webhooks/pagerduty/{agent_id}
    POST   /webhooks/alertmanager/{agent_id}
    POST   /webhooks/generic/{agent_id}    — passthrough body
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, StreamingResponse
from sqlalchemy import func, select

from datetime import datetime, timezone

from ..config import get_settings
from ..connectors import register_builtin_connectors
from ..connectors.base import registry as conn_registry
from ..db import init_db, session
from ..events import get_bus
from ..limits import check_dedup, check_rate_limit, record_dedup
from ..models import Agent, Approval, ApprovalStatus, Event, Run, RunStatus
from ..orchestration.executor import (
    cancel_run,
    execute_agent,
    resume_run,
    retry_run,
    sweep_expired_approvals,
)
from ..templates import register_builtin_templates
from ..templates.base import registry as tpl_registry
from .schemas import (
    AgentCreate,
    AgentOut,
    AgentUpdate,
    ApprovalOut,
    ApprovalResolve,
    EventOut,
    RunOut,
    TemplateOut,
    TriggerRequest,
)

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    register_builtin_connectors()
    register_builtin_templates()
    await init_db()

    # Plugin manager — discovers and activates everything: built-ins,
    # entry-point plugins, and directory-dropped plugins. Built-ins are
    # also already in the registries via the back-compat shim above;
    # the manager's idempotent registration is a no-op for those.
    from ..plugins.manager import get_manager

    plugin_manager = get_manager()
    await plugin_manager.load_all()

    # Re-attach persisted MCP servers (best-effort — a failing MCP server
    # shouldn't keep the API from coming up).
    from ..connectors.mcp import McpServerSpec, register_mcp_server
    from ..models import McpServer

    async with session() as s:
        rows = (await s.execute(select(McpServer).where(McpServer.enabled == True))).scalars().all()  # noqa: E712
        for row in rows:
            try:
                await register_mcp_server(
                    McpServerSpec(
                        name=row.name,
                        transport=row.transport or "stdio",
                        command=list(row.command or []),
                        env=dict(row.env or {}),
                        url=row.url or "",
                        headers=dict(row.headers or {}),
                        destructive_tools=set(row.destructive_tools or []),
                        description=row.description,
                    )
                )
            except Exception as exc:
                log.warning("MCP server '%s' failed to attach: %s", row.name, exc)

    # Start scheduler lazily — avoids hard dep at import time for tests.
    from ..scheduler import get_scheduler

    sched = get_scheduler()
    await sched.start()
    sweeper = asyncio.create_task(_ttl_sweeper_loop())
    from ..notify import start_notifier, stop_notifier

    start_notifier()
    try:
        yield
    finally:
        stop_notifier()
        sweeper.cancel()
        try:
            await sweeper
        except (asyncio.CancelledError, Exception):
            pass
        await sched.stop()
        try:
            await plugin_manager.shutdown()
        except Exception:
            log.exception("plugin manager shutdown failed")
        # Tear down the executor — for Temporal mode this stops the
        # worker + (in dev mode) the embedded server; for in-process
        # it's a no-op so the call is safe either way.
        try:
            from ..orchestration.executor import _get_executor

            ex = _get_executor()
            if hasattr(ex, "shutdown"):
                await ex.shutdown()
        except Exception:
            log.exception("executor shutdown failed")


async def _ttl_sweeper_loop() -> None:
    """Background task: periodically expire stale approvals."""
    s = get_settings()
    interval = max(5, int(s.approval_sweep_interval_seconds))
    while True:
        try:
            await sweep_expired_approvals()
        except Exception:  # pragma: no cover — never crash the loop
            log.exception("approval TTL sweep failed")
        await asyncio.sleep(interval)


_PUBLIC_PATHS = {"/healthz", "/metrics"}


def _is_public(path: str) -> bool:
    return path in _PUBLIC_PATHS or path.startswith("/webhooks/")


async def _resolve_tenant(request: Request) -> str:
    """Resolve the inbound request to a tenant_id.

    Resolution rules:

      1. Public paths skip auth entirely; they get the default tenant
         (used only for `/metrics` aggregations across tenants).
      2. If a tenant exposes any api_keys, a request matching one of
         them is bound to that tenant.
      3. If NO tenant has api_keys configured, the system runs in
         single-tenant dev mode and every request routes to the default.
      4. Otherwise: 401.

    The function is intentionally async — it touches the DB. Production
    deployments that want sub-millisecond auth should add a Redis cache
    of `key → tenant_id` here (the tenants module's `resolve_tenant_for_key`
    is the only call site to upgrade).
    """
    from ..tenants import ensure_default_tenant, list_tenants, resolve_tenant_for_key

    if _is_public(request.url.path):
        return await ensure_default_tenant()

    tenants = await list_tenants()
    any_keys = any(t.api_keys for t in tenants)
    if not any_keys:
        return await ensure_default_tenant()

    auth = request.headers.get("authorization", "")
    provided = ""
    if auth.lower().startswith("bearer "):
        provided = auth[7:].strip()
    if not provided:
        provided = request.headers.get("x-api-key", "").strip()
    if not provided:
        raise HTTPException(status_code=401, detail="missing API key")
    t = await resolve_tenant_for_key(provided)
    if t is None:
        raise HTTPException(status_code=401, detail="invalid API key")
    return t.id


def current_tenant(request: Request) -> str:
    """FastAPI dependency: returns the tenant_id stamped by the middleware."""
    tid = getattr(request.state, "tenant_id", None)
    if not tid:
        # Should never happen — middleware always stamps. Defensive guard.
        raise HTTPException(500, "tenant context missing")
    return tid


_OPENAPI_TAGS = [
    {"name": "meta", "description": "Health, identity, templates, connectors."},
    {"name": "agents", "description": "CRUD for agent definitions."},
    {"name": "runs", "description": "Run lifecycle: trigger, search, cancel, retry, audit export, SSE."},
    {"name": "approvals", "description": "Human-in-the-loop approval gates."},
    {"name": "manifest", "description": "Declarative GitOps: plan / apply / export."},
    {"name": "stats", "description": "Aggregated runs / tokens / cost for dashboards."},
    {"name": "schedule", "description": "Cron-driven agents and next-fire times."},
    {"name": "plugins", "description": "Plugin discovery and enable/disable."},
    {"name": "webhooks", "description": "Inbound webhooks (Datadog, GitHub, PagerDuty, Slack, …)."},
    {"name": "metrics", "description": "Prometheus-format scrape endpoint."},
]


def build_app() -> FastAPI:
    app = FastAPI(
        title="Pilothouse",
        version="0.1.0",
        description=(
            "AI DevOps Copilot platform. Configure agents once, let them "
            "run on cron / webhook / API triggers across CI/CD, monitoring "
            "and IaC. Source: https://github.com/example/pilothouse"
        ),
        lifespan=lifespan,
        openapi_tags=_OPENAPI_TAGS,
    )

    # The Next.js console runs on a separate origin in dev; allow it
    # explicitly. Production deployments terminate behind a reverse proxy
    # so CORS becomes a no-op.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ],
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=False,
    )

    @app.middleware("http")
    async def _auth_middleware(request: Request, call_next):
        try:
            tid = await _resolve_tenant(request)
        except HTTPException as exc:
            from fastapi.responses import JSONResponse

            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        request.state.tenant_id = tid
        return await call_next(request)

    @app.get("/healthz", tags=["meta"])
    async def healthz() -> dict:
        return {"ok": True}

    @app.get("/me", tags=["meta"])
    async def me(tenant_id: str = Depends(current_tenant)) -> dict:
        """Tell the caller which tenant their key (or anonymous session)
        resolved to. The console reads this to render the tenant indicator
        in the nav."""
        from ..models import Tenant

        async with session() as s:
            t = (await s.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one()
        return {
            "tenant_id": t.id,
            "tenant_name": t.name,
            "tenant_display_name": t.display_name,
        }

    @app.get("/metrics", response_class=PlainTextResponse, tags=["metrics"])
    async def metrics() -> str:
        s = get_settings()
        if not s.metrics_enabled:
            raise HTTPException(404, "metrics disabled")
        bus = get_bus()
        # Snapshot counters from the in-process bus plus a few DB-derived
        # gauges. Exposition format is Prometheus text, single-process —
        # scrape with `prometheus.yml` job targeting /metrics.
        lines: list[str] = []

        def metric(name: str, help_text: str, mtype: str, samples: list[tuple[str, float]]) -> None:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {mtype}")
            for labels, value in samples:
                lines.append(f"{name}{labels} {value}")

        metric(
            "pilothouse_events_total",
            "Total events emitted by the runtime, by kind.",
            "counter",
            [(f'{{kind="{k}"}}', v) for k, v in bus.event_counts.items()],
        )
        metric(
            "pilothouse_tool_invocations_total",
            "Tool invocations, by tool name.",
            "counter",
            [(f'{{tool="{k}"}}', v) for k, v in bus.tool_invocations.items()],
        )
        metric(
            "pilothouse_run_status_total",
            "Run terminations by terminal status.",
            "counter",
            [(f'{{status="{k}"}}', v) for k, v in bus.run_status_counts.items()],
        )
        metric(
            "pilothouse_approvals_resolved_total",
            "Approval resolutions, by decision.",
            "counter",
            [(f'{{decision="{k}"}}', v) for k, v in bus.approvals_resolved.items()],
        )

        # DB-derived gauges (cheap with indexes on small tables).
        async with session() as sess:
            agents_total = (await sess.execute(select(func.count(Agent.id)))).scalar_one()
            pending_approvals = (
                await sess.execute(
                    select(func.count(Approval.id)).where(
                        Approval.status == ApprovalStatus.pending
                    )
                )
            ).scalar_one()
            awaiting_runs = (
                await sess.execute(
                    select(func.count(Run.id)).where(
                        Run.status == RunStatus.awaiting_approval
                    )
                )
            ).scalar_one()

        metric(
            "pilothouse_agents", "Configured agents.", "gauge", [("", agents_total)]
        )
        metric(
            "pilothouse_approvals_pending",
            "Approvals currently pending.",
            "gauge",
            [("", pending_approvals)],
        )
        metric(
            "pilothouse_runs_awaiting_approval",
            "Runs paused at an approval gate.",
            "gauge",
            [("", awaiting_runs)],
        )
        return "\n".join(lines) + "\n"

    # --- meta -----------------------------------------------------------

    @app.get("/templates", response_model=list[TemplateOut], tags=["meta"])
    async def list_templates() -> list[TemplateOut]:
        return [
            TemplateOut(
                key=t.key, name=t.name, description=t.description, default_tools=t.default_tools
            )
            for t in tpl_registry.all()
        ]

    @app.get("/connectors", tags=["meta"])
    async def list_connectors() -> list[dict]:
        out: list[dict] = []
        for c in conn_registry.connectors.values():
            out.append(
                {
                    "name": c.name,
                    "live": c.live,
                    "tools": [
                        {"name": t.name, "destructive": t.is_destructive}
                        for t in c.tools()
                    ],
                }
            )
        return out

    # --- agents ---------------------------------------------------------

    async def _own_agent(s, agent_id: str, tenant_id: str) -> Agent:
        """Return the agent if it exists AND belongs to this tenant."""
        a = (
            await s.execute(
                select(Agent).where(Agent.id == agent_id, Agent.tenant_id == tenant_id)
            )
        ).scalar_one_or_none()
        if a is None:
            raise HTTPException(404, "agent not found")
        return a

    async def _own_run(s, run_id: str, tenant_id: str) -> Run:
        r = (
            await s.execute(
                select(Run).where(Run.id == run_id, Run.tenant_id == tenant_id)
            )
        ).scalar_one_or_none()
        if r is None:
            raise HTTPException(404, "run not found")
        return r

    @app.post("/agents", tags=["agents"], response_model=AgentOut, status_code=status.HTTP_201_CREATED)
    async def create_agent(
        body: AgentCreate, tenant_id: str = Depends(current_tenant)
    ) -> AgentOut:
        if body.template not in tpl_registry.templates:
            raise HTTPException(400, f"unknown template: {body.template}")
        from ..tenants import check_agent_quota

        ok, reason = await check_agent_quota(tenant_id)
        if not ok:
            raise HTTPException(403, reason or "agent quota exceeded")
        async with session() as s:
            existing = (
                await s.execute(
                    select(Agent).where(
                        Agent.name == body.name, Agent.tenant_id == tenant_id
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                raise HTTPException(409, "agent name already exists in this tenant")
            a = Agent(
                tenant_id=tenant_id,
                name=body.name,
                template=body.template,
                description=body.description,
                params=body.params,
                schedule_cron=body.schedule_cron,
                enabled=body.enabled,
                dry_run=body.dry_run,
            )
            s.add(a)
            await s.flush()
            if a.schedule_cron and a.enabled:
                from ..scheduler import get_scheduler

                await get_scheduler().add_or_update(a.id, a.schedule_cron)
            return AgentOut.model_validate(a)

    @app.get("/agents", tags=["agents"], response_model=list[AgentOut])
    async def list_agents(tenant_id: str = Depends(current_tenant)) -> list[AgentOut]:
        async with session() as s:
            rows = (
                await s.execute(
                    select(Agent)
                    .where(Agent.tenant_id == tenant_id)
                    .order_by(Agent.created_at.desc())
                )
            ).scalars().all()
            return [AgentOut.model_validate(a) for a in rows]

    @app.get("/agents/{agent_id}", tags=["agents"], response_model=AgentOut)
    async def get_agent(
        agent_id: str, tenant_id: str = Depends(current_tenant)
    ) -> AgentOut:
        async with session() as s:
            a = await _own_agent(s, agent_id, tenant_id)
            return AgentOut.model_validate(a)

    @app.patch("/agents/{agent_id}", tags=["agents"], response_model=AgentOut)
    async def update_agent(
        agent_id: str, body: AgentUpdate, tenant_id: str = Depends(current_tenant)
    ) -> AgentOut:
        async with session() as s:
            a = await _own_agent(s, agent_id, tenant_id)
            for field, value in body.model_dump(exclude_unset=True).items():
                setattr(a, field, value)
            await s.flush()
            from ..scheduler import get_scheduler

            sched = get_scheduler()
            if a.schedule_cron and a.enabled:
                await sched.add_or_update(a.id, a.schedule_cron)
            else:
                await sched.remove(a.id)
            return AgentOut.model_validate(a)

    @app.delete("/agents/{agent_id}", tags=["agents"], status_code=status.HTTP_204_NO_CONTENT)
    async def delete_agent(
        agent_id: str, tenant_id: str = Depends(current_tenant)
    ) -> None:
        async with session() as s:
            a = await _own_agent(s, agent_id, tenant_id)
            await s.delete(a)
        from ..scheduler import get_scheduler

        await get_scheduler().remove(agent_id)

    @app.post("/agents/{agent_id}/trigger", tags=["agents"], response_model=RunOut)
    async def trigger_run(
        agent_id: str,
        body: TriggerRequest,
        background: BackgroundTasks,
        tenant_id: str = Depends(current_tenant),
    ) -> RunOut:
        async with session() as s:
            await _own_agent(s, agent_id, tenant_id)

        s_cfg = get_settings()
        if not await check_rate_limit(tenant_id, limit_per_minute=s_cfg.rate_limit_per_minute):
            raise HTTPException(429, "tenant rate limit exceeded")
        from ..tenants import check_run_quota

        ok, reason = await check_run_quota(tenant_id)
        if not ok:
            raise HTTPException(429, reason or "tenant run quota exceeded")
        is_dup, existing_id = await check_dedup(
            tenant_id, agent_id, body.payload, window_seconds=s_cfg.dedup_window_seconds
        )
        if is_dup and existing_id:
            async with session() as s:
                r = (await s.execute(select(Run).where(Run.id == existing_id))).scalar_one()
                return RunOut.model_validate(r)

        run_id = await execute_agent(
            agent_id=agent_id,
            trigger="manual",
            trigger_payload=body.payload,
            dry_run_override=body.dry_run,
        )
        await record_dedup(
            tenant_id, agent_id, body.payload, run_id,
            window_seconds=s_cfg.dedup_window_seconds,
        )
        async with session() as s:
            r = (await s.execute(select(Run).where(Run.id == run_id))).scalar_one()
            return RunOut.model_validate(r)

    @app.get("/agents/{agent_id}/runs", tags=["agents"], response_model=list[RunOut])
    async def list_runs(
        agent_id: str,
        limit: int = 20,
        tenant_id: str = Depends(current_tenant),
    ) -> list[RunOut]:
        async with session() as s:
            await _own_agent(s, agent_id, tenant_id)
            rows = (
                await s.execute(
                    select(Run)
                    .where(Run.agent_id == agent_id, Run.tenant_id == tenant_id)
                    .order_by(Run.started_at.desc())
                    .limit(limit)
                )
            ).scalars().all()
            return [RunOut.model_validate(r) for r in rows]

    @app.get("/runs", tags=["runs"], response_model=list[RunOut])
    async def search_runs(
        status: str | None = None,
        agent: str | None = None,
        trigger: str | None = None,
        q: str | None = None,
        limit: int = 50,
        offset: int = 0,
        tenant_id: str = Depends(current_tenant),
    ) -> list[RunOut]:
        """Cross-agent run search, scoped to the calling tenant.

        Filters compose:
          status   — exact RunStatus value (succeeded/failed/...)
          agent    — exact agent name
          trigger  — substring match against Run.trigger (e.g. "webhook")
          q        — substring match against Run.summary

        Pagination via limit + offset; tested up to a few thousand runs
        on SQLite without indexes beyond the defaults.
        """
        from sqlalchemy import or_

        async with session() as s:
            stmt = (
                select(Run)
                .where(Run.tenant_id == tenant_id)
                .order_by(Run.started_at.desc())
                .limit(max(1, min(500, limit)))
                .offset(max(0, offset))
            )
            if status:
                try:
                    stmt = stmt.where(Run.status == RunStatus(status))
                except ValueError as exc:
                    raise HTTPException(400, f"unknown status: {status}") from exc
            if trigger:
                stmt = stmt.where(Run.trigger.like(f"%{trigger}%"))
            if q:
                stmt = stmt.where(Run.summary.like(f"%{q}%"))
            if agent:
                stmt = stmt.join(Agent, Agent.id == Run.agent_id).where(
                    Agent.name == agent
                )
            rows = (await s.execute(stmt)).scalars().all()
            return [RunOut.model_validate(r) for r in rows]

    @app.get("/runs/{run_id}", tags=["runs"], response_model=RunOut)
    async def get_run(
        run_id: str, tenant_id: str = Depends(current_tenant)
    ) -> RunOut:
        async with session() as s:
            r = await _own_run(s, run_id, tenant_id)
            return RunOut.model_validate(r)

    @app.get("/runs/{run_id}/events", tags=["runs"], response_model=list[EventOut])
    async def list_events(
        run_id: str, tenant_id: str = Depends(current_tenant)
    ) -> list[EventOut]:
        async with session() as s:
            await _own_run(s, run_id, tenant_id)
            rows = (
                await s.execute(
                    select(Event).where(Event.run_id == run_id).order_by(Event.created_at.asc())
                )
            ).scalars().all()
            return [EventOut.model_validate(e) for e in rows]

    @app.get("/runs/{run_id}/export.json", tags=["runs"])
    async def export_run_json(
        run_id: str, tenant_id: str = Depends(current_tenant)
    ):
        """Full audit trail of one run as a downloadable JSON file.

        Captures: run row + agent snapshot at export time + every event
        in chronological order + every approval. Designed for compliance
        archives — paste into a SOC2 ticket and you have the answer to
        "what did the AI do, when, with whose approval".
        """
        from fastapi.responses import JSONResponse

        async with session() as s:
            run = await _own_run(s, run_id, tenant_id)
            agent = (
                await s.execute(select(Agent).where(Agent.id == run.agent_id))
            ).scalar_one_or_none()
            events = (
                await s.execute(
                    select(Event).where(Event.run_id == run_id).order_by(Event.created_at.asc())
                )
            ).scalars().all()
            approvals = (
                await s.execute(
                    select(Approval)
                    .where(Approval.run_id == run_id)
                    .order_by(Approval.created_at.asc())
                )
            ).scalars().all()

        bundle = {
            "run": RunOut.model_validate(run).model_dump(mode="json"),
            "agent_snapshot": (
                AgentOut.model_validate(agent).model_dump(mode="json") if agent else None
            ),
            "events": [EventOut.model_validate(e).model_dump(mode="json") for e in events],
            "approvals": [
                ApprovalOut.model_validate(a).model_dump(mode="json") for a in approvals
            ],
        }
        return JSONResponse(
            bundle,
            headers={
                "Content-Disposition": f'attachment; filename="run-{run_id[:8]}.json"',
            },
        )

    @app.get("/runs/{run_id}/export.csv", tags=["runs"])
    async def export_run_csv(
        run_id: str, tenant_id: str = Depends(current_tenant)
    ):
        """Event timeline as CSV — one row per event.

        CSV is intentionally simple: for the rich JSON-shaped event
        `data` we drop the JSON serialization into a single column.
        Spreadsheets handle that fine; if you need structured access
        use export.json instead.
        """
        import csv
        import io

        from fastapi.responses import StreamingResponse

        async with session() as s:
            await _own_run(s, run_id, tenant_id)
            events = (
                await s.execute(
                    select(Event).where(Event.run_id == run_id).order_by(Event.created_at.asc())
                )
            ).scalars().all()

        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["created_at", "kind", "data_json"])
        for e in events:
            kind = e.kind.value if hasattr(e.kind, "value") else str(e.kind)
            w.writerow([e.created_at.isoformat(), kind, _json.dumps(e.data, default=str)])
        buf.seek(0)

        async def stream():
            yield buf.read()

        return StreamingResponse(
            stream(),
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="run-{run_id[:8]}.csv"',
            },
        )

    @app.get("/runs/{run_id}/events/stream", tags=["runs"])
    async def stream_events(
        run_id: str,
        request: Request,
        tenant_id: str = Depends(current_tenant),
    ) -> StreamingResponse:
        """Server-Sent Events stream of run activity.

        Tenant-scoped: a 404 is sent down the stream as `event: error`
        if the run doesn't exist OR doesn't belong to this tenant. We
        return the StreamingResponse either way (rather than raising)
        so an EventSource client can render the error consistently.
        """

        async def gen():
            # Replay history first.
            seen_ids: set[str] = set()
            async with session() as s:
                rows = (
                    await s.execute(
                        select(Event)
                        .where(Event.run_id == run_id)
                        .order_by(Event.created_at.asc())
                    )
                ).scalars().all()
                run = (
                    await s.execute(
                        select(Run).where(
                            Run.id == run_id, Run.tenant_id == tenant_id
                        )
                    )
                ).scalar_one_or_none()
                if run is None:
                    yield f"event: error\ndata: {_json.dumps({'detail': 'run not found'})}\n\n"
                    return
                for ev in rows:
                    seen_ids.add(ev.id)
                    payload = {
                        "id": ev.id,
                        "kind": ev.kind.value if hasattr(ev.kind, "value") else str(ev.kind),
                        "data": ev.data,
                        "created_at": ev.created_at.isoformat(),
                    }
                    yield f"event: {payload['kind']}\ndata: {_json.dumps(payload, default=str)}\n\n"
                terminal_now = run.status in (
                    RunStatus.succeeded,
                    RunStatus.failed,
                    RunStatus.cancelled,
                )
                current_status = run.status.value if hasattr(run.status, "value") else str(run.status)
            if terminal_now:
                yield f"event: end\ndata: {_json.dumps({'status': current_status})}\n\n"
                return

            # Live subscription — events arrive on the bus as the
            # runtime emits them. We drop out when the run reaches a
            # terminal kind or the client disconnects.
            bus = get_bus()
            try:
                sub = bus.subscribe(run_id)
                async for live_ev in sub:
                    if await request.is_disconnected():
                        break
                    payload = live_ev.to_json()
                    yield f"event: {payload['kind']}\ndata: {_json.dumps(payload, default=str)}\n\n"
                    if payload["kind"] in {"run_finished", "run_cancelled"}:
                        yield "event: end\ndata: {}\n\n"
                        break
            except asyncio.CancelledError:
                return

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/runs/{run_id}/cancel", tags=["runs"], response_model=RunOut)
    async def cancel(
        run_id: str,
        body: dict | None = None,
        tenant_id: str = Depends(current_tenant),
    ) -> RunOut:
        async with session() as s:
            await _own_run(s, run_id, tenant_id)
        by = (body or {}).get("by", "operator") if isinstance(body, dict) else "operator"
        try:
            await cancel_run(run_id, by=by)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        async with session() as s:
            r = await _own_run(s, run_id, tenant_id)
            return RunOut.model_validate(r)

    @app.post("/runs/{run_id}/retry", tags=["runs"], response_model=RunOut)
    async def retry(
        run_id: str,
        body: dict | None = None,
        tenant_id: str = Depends(current_tenant),
    ) -> RunOut:
        """Replay a finished/cancelled run with the same trigger payload."""
        async with session() as s:
            await _own_run(s, run_id, tenant_id)
        dry_run_override = None
        if isinstance(body, dict) and "dry_run" in body:
            dry_run_override = bool(body["dry_run"])
        try:
            new_id = await retry_run(run_id, dry_run_override=dry_run_override)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        async with session() as s:
            r = (await s.execute(select(Run).where(Run.id == new_id))).scalar_one()
            return RunOut.model_validate(r)

    # --- schedule -------------------------------------------------------

    @app.get("/schedule", tags=["schedule"])
    async def schedule(tenant_id: str = Depends(current_tenant)) -> list[dict]:
        """List enabled scheduled agents for this tenant + next-fire time.

        next_fire is computed from the cron string with APScheduler's
        cron parser; it does not consult the running scheduler, so the
        endpoint is correct even if the scheduler hasn't started.
        """
        from ..scheduler import next_fire_time

        async with session() as s:
            rows = (
                await s.execute(
                    select(Agent).where(
                        Agent.tenant_id == tenant_id,
                        Agent.enabled == True,  # noqa: E712
                        Agent.schedule_cron.isnot(None),
                    )
                )
            ).scalars().all()
        out: list[dict] = []
        for a in rows:
            out.append(
                {
                    "id": a.id,
                    "name": a.name,
                    "template": a.template,
                    "cron": a.schedule_cron,
                    "next_fire": next_fire_time(a.schedule_cron) if a.schedule_cron else None,
                    "dry_run": a.dry_run,
                }
            )
        # Sort by next-fire, soonest first; entries with invalid cron last.
        out.sort(key=lambda x: x["next_fire"] or "9999-99")
        return out

    # --- stats ----------------------------------------------------------

    @app.get("/stats", tags=["stats"])
    async def stats(days: int = 7, tenant_id: str = Depends(current_tenant)) -> dict:
        """Aggregated run stats for the console dashboard.

        Buckets the last `days` of runs by date and by agent, returning
        token totals and run counts. SQLite can do this with one
        groupby — production Postgres scales the same query natively.
        """
        from datetime import datetime, timedelta, timezone

        cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, days))
        async with session() as s:
            rows = (
                await s.execute(
                    select(Run, Agent.name)
                    .join(Agent, Agent.id == Run.agent_id)
                    .where(Run.started_at >= cutoff, Run.tenant_id == tenant_id)
                    .order_by(Run.started_at.asc())
                )
            ).all()

        by_day: dict[str, dict] = {}
        by_agent: dict[str, dict] = {}
        by_status: dict[str, int] = {}
        total_in = total_out = total_cost = 0
        for r, agent_name in rows:
            day = r.started_at.strftime("%Y-%m-%d")
            d = by_day.setdefault(
                day,
                {"date": day, "runs": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0},
            )
            d["runs"] += 1
            d["tokens_in"] += r.tokens_input
            d["tokens_out"] += r.tokens_output
            d["cost_usd"] += r.cost_usd_cents / 10000.0

            a = by_agent.setdefault(
                agent_name,
                {"agent": agent_name, "runs": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0},
            )
            a["runs"] += 1
            a["tokens_in"] += r.tokens_input
            a["tokens_out"] += r.tokens_output
            a["cost_usd"] += r.cost_usd_cents / 10000.0

            status_v = r.status.value if hasattr(r.status, "value") else str(r.status)
            by_status[status_v] = by_status.get(status_v, 0) + 1

            total_in += r.tokens_input
            total_out += r.tokens_output
            total_cost += r.cost_usd_cents / 10000.0

        # Fill missing days with zeros so charts stay continuous.
        from datetime import date

        days_back = max(1, days)
        full: list[dict] = []
        for offset in range(days_back - 1, -1, -1):
            d = (datetime.now(timezone.utc) - timedelta(days=offset)).strftime("%Y-%m-%d")
            full.append(
                by_day.get(d, {"date": d, "runs": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0})
            )

        return {
            "window_days": days_back,
            "totals": {
                "runs": sum(1 for _ in rows),
                "tokens_in": total_in,
                "tokens_out": total_out,
                "cost_usd": round(total_cost, 4),
            },
            "by_day": full,
            "by_agent": sorted(by_agent.values(), key=lambda x: -x["cost_usd"]),
            "by_status": by_status,
        }

    # --- plugins --------------------------------------------------------

    async def _require_admin_tenant(tenant_id: str) -> None:
        """Plugins are system-level — only the default tenant may toggle.
        This is a deliberately coarse model (no per-tenant RBAC yet).
        """
        from ..tenants import ensure_default_tenant

        default_tid = await ensure_default_tenant()
        if tenant_id != default_tid:
            raise HTTPException(403, "plugins require admin (default tenant) credentials")

    @app.get("/plugins", tags=["plugins"])
    async def list_plugins_endpoint(
        tenant_id: str = Depends(current_tenant),
    ) -> list[dict]:
        # Anyone can read the list (it's not sensitive — operators want
        # to see what's available). Only admins can mutate.
        from ..plugins.manager import get_manager

        return get_manager().list_plugins()

    @app.post("/plugins/{name}/enable", tags=["plugins"])
    async def enable_plugin_endpoint(
        name: str, tenant_id: str = Depends(current_tenant)
    ) -> dict:
        await _require_admin_tenant(tenant_id)
        from ..plugins.manager import get_manager

        try:
            await get_manager().enable(name)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"name": name, "enabled": True}

    @app.post("/plugins/{name}/disable", tags=["plugins"])
    async def disable_plugin_endpoint(
        name: str, tenant_id: str = Depends(current_tenant)
    ) -> dict:
        await _require_admin_tenant(tenant_id)
        from ..plugins.manager import get_manager

        try:
            await get_manager().disable(name)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"name": name, "enabled": False}

    @app.post("/plugins/reload", tags=["plugins"])
    async def reload_plugins_endpoint(
        tenant_id: str = Depends(current_tenant),
    ) -> dict:
        """Re-run discovery (entry points + directory). Useful after
        dropping a new .py into PILOTHOUSE_PLUGIN_DIR."""
        await _require_admin_tenant(tenant_id)
        from ..plugins.manager import get_manager

        mgr = get_manager()
        await mgr.load_all()
        return {"count": len(mgr.list_plugins())}

    @app.get("/plugins/doctor", tags=["plugins"])
    async def plugins_doctor_endpoint(
        tenant_id: str = Depends(current_tenant),
    ) -> dict:
        """Same as the CLI doctor — list of misconfigured plugins.
        Empty list = healthy."""
        from ..plugins.manager import get_manager

        return {"misconfigured": get_manager().doctor()}

    @app.get("/plugins/{name}/config", tags=["plugins"])
    async def get_plugin_config_endpoint(
        name: str,
        reveal: bool = False,
        tenant_id: str = Depends(current_tenant),
    ) -> dict:
        """Show resolved config. Secrets are masked unless `reveal=true`,
        and reveal requires admin tenant."""
        if reveal:
            await _require_admin_tenant(tenant_id)
        from ..plugins.manager import get_manager

        try:
            return await get_manager().get_config(name, mask_secrets=not reveal)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/plugins/{name}/config", tags=["plugins"])
    async def set_plugin_config_endpoint(
        name: str,
        body: dict,
        tenant_id: str = Depends(current_tenant),
    ) -> dict:
        """Body: `{"key": "...", "value": "..."}` to set, or
        `{"unset": "key"}` to clear. Re-activates the plugin in-place."""
        await _require_admin_tenant(tenant_id)
        from ..plugins.manager import get_manager

        mgr = get_manager()
        try:
            if "unset" in body:
                await mgr.unset_config(name, body["unset"])
                return {"name": name, "unset": body["unset"]}
            key = body.get("key")
            value = body.get("value", "")
            if not key:
                raise HTTPException(400, "missing 'key' or 'unset' in body")
            await mgr.set_config(name, key, value)
            return {"name": name, "set": key}
        except KeyError as exc:
            raise HTTPException(400, str(exc)) from exc

    # --- declarative apply ---------------------------------------------

    @app.post("/manifest/plan", tags=["manifest"])
    async def manifest_plan(
        body: dict, tenant_id: str = Depends(current_tenant)
    ) -> dict:
        """Compute a diff against the supplied manifest (no writes)."""
        from ..declarative import Manifest, compute_plan

        try:
            manifest = Manifest(**body)
        except Exception as exc:
            raise HTTPException(400, f"invalid manifest: {exc}") from exc
        plan = await compute_plan(manifest, tenant_id=tenant_id)
        return {
            "summary": plan.summary(),
            "items": [
                {"name": i.name, "action": i.action, "diff": i.diff} for i in plan.items
            ],
        }

    @app.post("/manifest/apply", tags=["manifest"])
    async def manifest_apply(
        body: dict, tenant_id: str = Depends(current_tenant)
    ) -> dict:
        from ..declarative import Manifest, apply_plan, compute_plan

        try:
            manifest = Manifest(**body)
        except Exception as exc:
            raise HTTPException(400, f"invalid manifest: {exc}") from exc
        plan = await compute_plan(manifest, tenant_id=tenant_id)
        await apply_plan(manifest, plan, tenant_id=tenant_id)
        return {
            "summary": plan.summary(),
            "applied": [
                {"name": i.name, "action": i.action} for i in plan.items if i.action != "noop"
            ],
        }

    @app.get("/manifest/export", tags=["manifest"])
    async def manifest_export(tenant_id: str = Depends(current_tenant)) -> dict:
        """Current state as a manifest payload — round-trips through /apply."""
        async with session() as s:
            rows = (
                await s.execute(
                    select(Agent)
                    .where(Agent.tenant_id == tenant_id)
                    .order_by(Agent.name)
                )
            ).scalars().all()
        return {
            "version": 1,
            "prune": False,
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

    # --- approvals ------------------------------------------------------

    @app.get("/approvals", tags=["approvals"], response_model=list[ApprovalOut])
    async def list_approvals(
        status: str | None = None,
        tool: str | None = None,
        agent: str | None = None,
        tenant_id: str = Depends(current_tenant),
    ) -> list[ApprovalOut]:
        async with session() as s:
            q = (
                select(Approval)
                .where(Approval.tenant_id == tenant_id)
                .order_by(Approval.created_at.desc())
            )
            if status:
                try:
                    enum_status = ApprovalStatus(status)
                except ValueError as exc:
                    raise HTTPException(400, str(exc)) from exc
                q = q.where(Approval.status == enum_status)
            if tool:
                q = q.where(Approval.tool_name == tool)
            if agent:
                q = q.join(Run, Run.id == Approval.run_id).join(
                    Agent, Agent.id == Run.agent_id
                ).where(Agent.name == agent)
            rows = (await s.execute(q)).scalars().all()
            return [ApprovalOut.model_validate(a) for a in rows]

    @app.get("/runs/{run_id}/approvals", tags=["runs"], response_model=list[ApprovalOut])
    async def list_approvals_for_run(
        run_id: str, tenant_id: str = Depends(current_tenant)
    ) -> list[ApprovalOut]:
        async with session() as s:
            await _own_run(s, run_id, tenant_id)
            rows = (
                await s.execute(
                    select(Approval)
                    .where(Approval.run_id == run_id, Approval.tenant_id == tenant_id)
                    .order_by(Approval.created_at.asc())
                )
            ).scalars().all()
            return [ApprovalOut.model_validate(a) for a in rows]

    @app.get("/approvals/{approval_id}", tags=["approvals"], response_model=ApprovalOut)
    async def get_approval(
        approval_id: str, tenant_id: str = Depends(current_tenant)
    ) -> ApprovalOut:
        async with session() as s:
            a = (
                await s.execute(
                    select(Approval).where(
                        Approval.id == approval_id, Approval.tenant_id == tenant_id
                    )
                )
            ).scalar_one_or_none()
            if a is None:
                raise HTTPException(404, "approval not found")
            return ApprovalOut.model_validate(a)

    async def _resolve_one(s, approval_id: str, decision: str, by: str, reason: str) -> tuple[str, bool]:
        a = (
            await s.execute(select(Approval).where(Approval.id == approval_id))
        ).scalar_one_or_none()
        if a is None:
            raise HTTPException(404, f"approval not found: {approval_id}")
        if a.status != ApprovalStatus.pending:
            raise HTTPException(409, f"approval {approval_id} already {a.status.value}")
        approved = decision in {"approve", "approved"}
        a.status = ApprovalStatus.approved if approved else ApprovalStatus.rejected
        a.resolved_by = by
        a.rejection_reason = "" if approved else reason
        a.resolved_at = datetime.now(timezone.utc)
        return a.run_id, approved

    async def _maybe_resume(run_id: str) -> None:
        async with session() as s:
            remaining = (
                await s.execute(
                    select(Approval).where(
                        Approval.run_id == run_id,
                        Approval.status == ApprovalStatus.pending,
                    )
                )
            ).scalars().first()
        if remaining is None:
            try:
                await resume_run(run_id)
            except Exception as exc:  # pragma: no cover — ops log
                log.exception("auto-resume failed for run %s: %s", run_id, exc)

    @app.post("/approvals/{approval_id}/resolve", tags=["approvals"], response_model=RunOut)
    async def resolve_approval(
        approval_id: str,
        body: ApprovalResolve,
        tenant_id: str = Depends(current_tenant),
    ) -> RunOut:
        decision = body.decision.lower()
        if decision not in {"approve", "approved", "reject", "rejected"}:
            raise HTTPException(400, "decision must be 'approve' or 'reject'")

        async with session() as s:
            # Tenant guard before mutation.
            owned = (
                await s.execute(
                    select(Approval).where(
                        Approval.id == approval_id, Approval.tenant_id == tenant_id
                    )
                )
            ).scalar_one_or_none()
            if owned is None:
                raise HTTPException(404, "approval not found")
            run_id, _ = await _resolve_one(s, approval_id, decision, body.resolved_by, body.reason)

        await _maybe_resume(run_id)
        async with session() as s:
            r = await _own_run(s, run_id, tenant_id)
            return RunOut.model_validate(r)

    @app.post("/approvals/resolve-batch", tags=["approvals"])
    async def resolve_batch(
        body: dict, tenant_id: str = Depends(current_tenant)
    ) -> dict:
        """Resolve many approvals in one shot.

        Body: { decision: "approve"|"reject", resolved_by, reason,
                ids?: [..], filters?: { status?, tool?, agent? } }

        If `ids` is omitted, the filters select pending approvals (and
        only pending — bulk operations always elide already-resolved
        rows to avoid surprises).
        """
        decision = (body.get("decision") or "").lower()
        if decision not in {"approve", "approved", "reject", "rejected"}:
            raise HTTPException(400, "decision must be 'approve' or 'reject'")
        by = body.get("resolved_by") or "operator"
        reason = body.get("reason") or ""
        ids: list[str] = list(body.get("ids") or [])
        filters = body.get("filters") or {}

        async with session() as s:
            if ids:
                q = select(Approval).where(
                    Approval.id.in_(ids), Approval.tenant_id == tenant_id
                )
            else:
                q = select(Approval).where(
                    Approval.tenant_id == tenant_id,
                    Approval.status == ApprovalStatus.pending,
                )
                if filters.get("tool"):
                    q = q.where(Approval.tool_name == filters["tool"])
                if filters.get("agent"):
                    q = q.join(Run, Run.id == Approval.run_id).join(
                        Agent, Agent.id == Run.agent_id
                    ).where(Agent.name == filters["agent"])
            candidates = (await s.execute(q)).scalars().all()

            results: list[dict] = []
            run_ids: set[str] = set()
            for a in candidates:
                if a.status != ApprovalStatus.pending:
                    results.append(
                        {"id": a.id, "ok": False, "error": f"already {a.status.value}"}
                    )
                    continue
                try:
                    rid, _ = await _resolve_one(s, a.id, decision, by, reason)
                    results.append({"id": a.id, "ok": True, "run_id": rid})
                    run_ids.add(rid)
                except HTTPException as exc:
                    results.append({"id": a.id, "ok": False, "error": exc.detail})

        for rid in run_ids:
            await _maybe_resume(rid)

        return {"resolved": results, "count": sum(1 for r in results if r["ok"])}

    # --- webhooks --------------------------------------------------------

    async def _verify_and_read(request: Request, source: str) -> tuple[dict, bytes]:
        """Source-specific signature verification + body parse.

        `source` is the webhook namespace (github / slack / pagerduty /
        datadog / alertmanager / generic). The verifier maps it to the
        correct header scheme and secret env var; an empty secret skips
        verification (dev mode).
        """
        from ..webhooks import WebhookVerificationError, verify

        raw = await request.body()
        # Headers are case-insensitive; normalise to lowercase keys.
        hdrs = {k.lower(): v for k, v in request.headers.items()}
        try:
            verify(source, hdrs, raw)
        except WebhookVerificationError as exc:
            raise HTTPException(401, f"webhook verification failed: {exc}") from exc
        try:
            payload = await request.json()
        except Exception:
            payload = {"raw": raw.decode("utf-8", errors="replace")}
        return payload, raw

    async def _enqueue(agent_id: str, trigger: str, payload: dict, background: BackgroundTasks):
        # Webhooks are unauthenticated (signature-verified instead), so
        # they don't have a tenant from the auth middleware. We pull
        # tenant_id off the agent row.
        async with session() as s:
            a = (await s.execute(select(Agent).where(Agent.id == agent_id))).scalar_one_or_none()
            if a is None:
                raise HTTPException(404, "agent not found")
            if not a.enabled:
                raise HTTPException(409, "agent disabled")
            tid = a.tenant_id or ""

        s_cfg = get_settings()
        if tid and not await check_rate_limit(tid, limit_per_minute=s_cfg.rate_limit_per_minute):
            raise HTTPException(429, "tenant rate limit exceeded")
        if tid:
            from ..tenants import check_run_quota

            ok, reason = await check_run_quota(tid)
            if not ok:
                raise HTTPException(429, reason or "tenant run quota exceeded")

        # Dedup webhook payloads inside the configured window. Datadog,
        # GitHub etc. retry on transient failures and the same incident
        # firing 5x in 30s shouldn't trigger 5 LLM runs.
        if tid:
            is_dup, existing_id = await check_dedup(
                tid, agent_id, payload,
                window_seconds=s_cfg.dedup_window_seconds,
            )
            if is_dup and existing_id:
                return {"accepted": True, "agent_id": agent_id, "deduped_to": existing_id}

        async def _run():
            try:
                run_id = await execute_agent(
                    agent_id=agent_id, trigger=trigger, trigger_payload=payload
                )
                if tid:
                    await record_dedup(
                        tid, agent_id, payload, run_id,
                        window_seconds=s_cfg.dedup_window_seconds,
                    )
            except Exception as exc:  # pragma: no cover — logged for ops
                log.exception("agent execution failed: %s", exc)

        background.add_task(asyncio.create_task, _run())
        return {"accepted": True, "agent_id": agent_id}

    @app.post("/webhooks/datadog/{agent_id}", tags=["webhooks"])
    async def webhook_datadog(agent_id: str, request: Request, background: BackgroundTasks):
        payload, _ = await _verify_and_read(request, "datadog")
        return await _enqueue(agent_id, "webhook:datadog", payload, background)

    @app.post("/webhooks/github/{agent_id}", tags=["webhooks"])
    async def webhook_github(agent_id: str, request: Request, background: BackgroundTasks):
        payload, _ = await _verify_and_read(request, "github")
        return await _enqueue(agent_id, "webhook:github", payload, background)

    @app.post("/webhooks/pagerduty/{agent_id}", tags=["webhooks"])
    async def webhook_pagerduty(agent_id: str, request: Request, background: BackgroundTasks):
        payload, _ = await _verify_and_read(request, "pagerduty")
        return await _enqueue(agent_id, "webhook:pagerduty", payload, background)

    # NOTE: register the more-specific `/interactivity` route BEFORE the
    # catch-all `/{agent_id}` so FastAPI matches it correctly.
    @app.post("/webhooks/slack/interactivity", tags=["webhooks"])
    async def webhook_slack_interactivity(request: Request) -> dict:
        """Slack action callback for interactive approval buttons.

        Slack sends `application/x-www-form-urlencoded` with a single
        `payload` field that contains JSON. We verify the v0 signature
        (same scheme as the other Slack webhooks), parse the payload,
        find the approval_id from the button `value`, resolve it, and
        update the original Slack message in-place to reflect the
        decision. Returning `{}` (empty body, 200) is enough for Slack
        — no `replace_original` needed because we use chat.update.
        """
        from urllib.parse import parse_qs

        # Read body + verify signature (only enforced when the secret is set).
        from ..webhooks import WebhookVerificationError, verify

        raw = await request.body()
        hdrs = {k.lower(): v for k, v in request.headers.items()}
        try:
            verify("slack", hdrs, raw)
        except WebhookVerificationError as exc:
            raise HTTPException(401, str(exc)) from exc

        parsed = parse_qs(raw.decode("utf-8"))
        payload_json = (parsed.get("payload") or [""])[0]
        try:
            payload = _json.loads(payload_json)
        except Exception as exc:
            raise HTTPException(400, f"bad slack payload: {exc}") from exc

        actions = payload.get("actions") or []
        if not actions:
            return {}
        action = actions[0]
        action_id = action.get("action_id") or ""
        approval_id = action.get("value") or ""
        decision = "approve" if action_id == "approve" else "reject"
        slack_user = (payload.get("user") or {}).get("username") or "slack"
        log.debug(
            "slack interactivity: action_id=%s approval_id=%s decision=%s",
            action_id, approval_id, decision,
        )

        async with session() as s:
            ap = (
                await s.execute(select(Approval).where(Approval.id == approval_id))
            ).scalar_one_or_none()
            if ap is None:
                raise HTTPException(404, "approval not found")
            if ap.status != ApprovalStatus.pending:
                # Update the Slack message anyway so the buttons go away.
                await _slack_update_message(payload, ap, decision="already-resolved")
                return {}
            from ..models import ApprovalStatus as _AS

            ap.status = _AS.approved if decision == "approve" else _AS.rejected
            ap.resolved_by = f"slack:{slack_user}"
            ap.rejection_reason = "" if decision == "approve" else "rejected from Slack"
            ap.resolved_at = datetime.now(timezone.utc)
            run_id = ap.run_id

            remaining = (
                await s.execute(
                    select(Approval).where(
                        Approval.run_id == run_id,
                        Approval.status == ApprovalStatus.pending,
                    )
                )
            ).scalars().first()
            should_resume = remaining is None

        if should_resume:
            try:
                await resume_run(run_id)
            except Exception as exc:  # pragma: no cover — ops log
                log.exception("auto-resume after slack action failed: %s", exc)

        await _slack_update_message(payload, ap, decision=decision)
        return {}

    async def _slack_update_message(payload: dict, approval: Approval, *, decision: str) -> None:
        """Replace the original interactive message with a status block
        so the buttons disappear and history reflects who decided."""
        import httpx

        token = get_settings().slack_bot_token
        if not token:
            return
        channel = (payload.get("channel") or {}).get("id")
        ts = (payload.get("message") or {}).get("ts")
        if not channel or not ts:
            return
        verb = {
            "approve": "approved",
            "reject": "rejected",
            "already-resolved": f"already {approval.status.value}",
        }.get(decision, decision)
        new_text = (
            f":white_check_mark: *{verb}* — `{approval.tool_name}` "
            f"by `{approval.resolved_by or 'slack'}`."
        )
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": new_text}}]
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    "https://slack.com/api/chat.update",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"channel": channel, "ts": ts, "text": new_text, "blocks": blocks},
                )
        except Exception:  # pragma: no cover
            log.exception("slack chat.update failed")

    @app.post("/webhooks/slack/{agent_id}", tags=["webhooks"])
    async def webhook_slack(agent_id: str, request: Request, background: BackgroundTasks):
        payload, _ = await _verify_and_read(request, "slack")
        return await _enqueue(agent_id, "webhook:slack", payload, background)

    @app.post("/webhooks/alertmanager/{agent_id}", tags=["webhooks"])
    async def webhook_alertmanager(agent_id: str, request: Request, background: BackgroundTasks):
        payload, _ = await _verify_and_read(request, "alertmanager")
        return await _enqueue(agent_id, "webhook:alertmanager", payload, background)

    @app.post("/webhooks/generic/{agent_id}", tags=["webhooks"])
    async def webhook_generic(agent_id: str, request: Request, background: BackgroundTasks):
        payload, _ = await _verify_and_read(request, "generic")
        return await _enqueue(agent_id, "webhook:generic", payload, background)

    return app
