"""Tenant resolution + bootstrap.

Multi-tenancy in Pilothouse is opt-in by configuration, not by code path.
The same code runs whether you have one tenant or fifty:

- On first start (or `pilothouse db init`) we ensure a tenant named
  "default" exists. Its api_keys come from `PILOTHOUSE_API_KEYS` (legacy
  flat env var) so existing deployments keep working.
- Existing rows with `tenant_id IS NULL` are backfilled to the default
  tenant — that's the migration story.
- The auth middleware looks up the inbound API key against every
  tenant's `api_keys`. If no tenants have any keys configured, requests
  route to the default tenant (anonymous mode, dev-friendly).

There is no "system" / "admin" tenant. Tenants are administered with
the CLI on the machine running Pilothouse — `pilothouse tenants ...` —
not over HTTP, so a compromised tenant key can never escalate.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import select, update

from .config import get_settings
from .db import session
from .models import Agent, Approval, McpServer, Run, Tenant, DEFAULT_TENANT_NAME

log = logging.getLogger(__name__)


_default_tenant_id_cache: Optional[str] = None


async def ensure_default_tenant() -> str:
    """Create the default tenant if missing and return its id.

    Each call (a) creates the tenant if absent, (b) merges any
    `PILOTHOUSE_API_KEYS` env-derived keys into the default tenant's
    `api_keys` list — so the legacy env var keeps working even if the
    DB already exists, and so changes to the env var after a restart
    take effect. The id is cached process-wide for the hot middleware
    path.
    """
    global _default_tenant_id_cache

    env_keys = sorted(get_settings().parsed_api_keys())

    async with session() as s:
        existing = (
            await s.execute(select(Tenant).where(Tenant.name == DEFAULT_TENANT_NAME))
        ).scalar_one_or_none()
        if existing is None:
            t = Tenant(
                name=DEFAULT_TENANT_NAME,
                display_name="Default",
                api_keys=list(env_keys),
            )
            s.add(t)
            await s.flush()
            _default_tenant_id_cache = t.id
        else:
            # Merge env keys into the existing default tenant. Idempotent.
            current = list(existing.api_keys or [])
            merged = list(current)
            for k in env_keys:
                if k not in merged:
                    merged.append(k)
            if merged != current:
                existing.api_keys = merged
                await s.flush()
            _default_tenant_id_cache = existing.id

    await _backfill_tenant_ids(_default_tenant_id_cache)
    return _default_tenant_id_cache


async def _backfill_tenant_ids(default_tenant_id: str) -> None:
    """One-shot backfill: any row with NULL tenant_id gets the default."""
    async with session() as s:
        await s.execute(
            update(Agent)
            .where(Agent.tenant_id.is_(None))
            .values(tenant_id=default_tenant_id)
        )
        await s.execute(
            update(Run)
            .where(Run.tenant_id.is_(None))
            .values(tenant_id=default_tenant_id)
        )
        await s.execute(
            update(Approval)
            .where(Approval.tenant_id.is_(None))
            .values(tenant_id=default_tenant_id)
        )
        await s.execute(
            update(McpServer)
            .where(McpServer.tenant_id.is_(None))
            .values(tenant_id=default_tenant_id)
        )


def reset_default_tenant_cache() -> None:
    """Test helper — drop cached default tenant id."""
    global _default_tenant_id_cache
    _default_tenant_id_cache = None


async def resolve_tenant_for_key(api_key: str) -> Optional[Tenant]:
    """Find the tenant whose api_keys include this token.

    O(N) over tenants — fine for MVP scale. A real install would index
    keys in a separate table or front this with Redis. The function is
    structured so swapping the backend is a single-call substitution.
    """
    async with session() as s:
        rows = (await s.execute(select(Tenant))).scalars().all()
    for t in rows:
        if api_key in (t.api_keys or []):
            return t
    return None


async def list_tenants() -> list[Tenant]:
    async with session() as s:
        rows = (await s.execute(select(Tenant).order_by(Tenant.created_at))).scalars().all()
        return list(rows)


async def get_tenant(name: str) -> Optional[Tenant]:
    async with session() as s:
        return (
            await s.execute(select(Tenant).where(Tenant.name == name))
        ).scalar_one_or_none()


async def create_tenant(name: str, display_name: str = "") -> Tenant:
    if await get_tenant(name) is not None:
        raise ValueError(f"tenant already exists: {name}")
    async with session() as s:
        t = Tenant(name=name, display_name=display_name or name, api_keys=[])
        s.add(t)
        await s.flush()
        return t


async def add_api_key(tenant_name: str, api_key: str) -> Tenant:
    async with session() as s:
        t = (
            await s.execute(select(Tenant).where(Tenant.name == tenant_name))
        ).scalar_one_or_none()
        if t is None:
            raise KeyError(f"unknown tenant: {tenant_name}")
        keys = list(t.api_keys or [])
        if api_key not in keys:
            keys.append(api_key)
            t.api_keys = keys
        await s.flush()
        return t


async def set_quota(tenant_name: str, *, max_agents: int | None = None, max_runs_per_day: int | None = None) -> Tenant:
    """Update one or both quota fields on a tenant. None = leave unchanged."""
    async with session() as s:
        t = (
            await s.execute(select(Tenant).where(Tenant.name == tenant_name))
        ).scalar_one_or_none()
        if t is None:
            raise KeyError(f"unknown tenant: {tenant_name}")
        if max_agents is not None:
            t.max_agents = max(0, int(max_agents))
        if max_runs_per_day is not None:
            t.max_runs_per_day = max(0, int(max_runs_per_day))
        await s.flush()
        return t


async def get_tenant_by_id(tenant_id: str) -> Tenant | None:
    async with session() as s:
        return (
            await s.execute(select(Tenant).where(Tenant.id == tenant_id))
        ).scalar_one_or_none()


async def check_agent_quota(tenant_id: str) -> tuple[bool, str | None]:
    """Return (allowed, reason). Called before agent create."""
    from .models import Agent

    t = await get_tenant_by_id(tenant_id)
    if t is None or t.max_agents <= 0:
        return True, None
    async with session() as s:
        from sqlalchemy import func

        count = (
            await s.execute(
                select(func.count(Agent.id)).where(Agent.tenant_id == tenant_id)
            )
        ).scalar_one()
    if count >= t.max_agents:
        return False, f"tenant agent quota reached ({t.max_agents})"
    return True, None


async def check_run_quota(tenant_id: str) -> tuple[bool, str | None]:
    """Return (allowed, reason). Called before triggering a run."""
    from datetime import datetime, timedelta, timezone

    from .models import Run

    t = await get_tenant_by_id(tenant_id)
    if t is None or t.max_runs_per_day <= 0:
        return True, None
    cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    async with session() as s:
        from sqlalchemy import func

        count = (
            await s.execute(
                select(func.count(Run.id)).where(
                    Run.tenant_id == tenant_id, Run.started_at >= cutoff
                )
            )
        ).scalar_one()
    if count >= t.max_runs_per_day:
        return False, f"tenant daily run quota reached ({t.max_runs_per_day})"
    return True, None


async def remove_api_key(tenant_name: str, api_key: str) -> Tenant:
    async with session() as s:
        t = (
            await s.execute(select(Tenant).where(Tenant.name == tenant_name))
        ).scalar_one_or_none()
        if t is None:
            raise KeyError(f"unknown tenant: {tenant_name}")
        t.api_keys = [k for k in (t.api_keys or []) if k != api_key]
        await s.flush()
        return t


async def delete_tenant(name: str) -> bool:
    """Delete tenant + cascade owned agents/runs/approvals/mcp servers.

    Refuses to delete "default" (single-tenant deployments must always
    have it). Cascade is explicit at this layer so the FK constraint
    isn't required to be ON DELETE CASCADE in every backend.
    """
    if name == DEFAULT_TENANT_NAME:
        raise ValueError("cannot delete the default tenant")
    async with session() as s:
        t = (
            await s.execute(select(Tenant).where(Tenant.name == name))
        ).scalar_one_or_none()
        if t is None:
            return False
        # Cascade — order matters because of FKs.
        agents = (
            await s.execute(select(Agent).where(Agent.tenant_id == t.id))
        ).scalars().all()
        for a in agents:
            await s.delete(a)
        mcp = (
            await s.execute(select(McpServer).where(McpServer.tenant_id == t.id))
        ).scalars().all()
        for m in mcp:
            await s.delete(m)
        await s.delete(t)
        return True
