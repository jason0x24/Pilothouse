"""Cron scheduler — APScheduler-backed.

One scheduler instance per process. Agents with a non-empty schedule_cron
get a job that fires `execute_agent` with `trigger="cron"`. On startup the
scheduler re-syncs from the DB so cron survives restarts.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from .db import session
from .models import Agent
from .orchestration.executor import execute_agent

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self) -> None:
        self._scheduler: Optional[AsyncIOScheduler] = None

    async def start(self) -> None:
        if self._scheduler is not None:
            return
        self._scheduler = AsyncIOScheduler()
        self._scheduler.start()
        await self._sync_from_db()

    async def stop(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    async def _sync_from_db(self) -> None:
        async with session() as s:
            rows = (await s.execute(select(Agent))).scalars().all()
            for a in rows:
                if a.enabled and a.schedule_cron:
                    self._add_job(a.id, a.schedule_cron)

    async def add_or_update(self, agent_id: str, cron: str) -> None:
        if self._scheduler is None:
            return
        self._add_job(agent_id, cron)

    async def remove(self, agent_id: str) -> None:
        if self._scheduler is None:
            return
        job_id = f"agent:{agent_id}"
        try:
            self._scheduler.remove_job(job_id)
        except Exception:
            pass

    def _add_job(self, agent_id: str, cron: str) -> None:
        assert self._scheduler is not None
        try:
            trigger = CronTrigger.from_crontab(cron)
        except Exception as exc:
            log.warning("agent %s has invalid cron %r: %s", agent_id, cron, exc)
            return
        job_id = f"agent:{agent_id}"
        self._scheduler.add_job(
            _fire_agent,
            trigger=trigger,
            id=job_id,
            args=[agent_id],
            replace_existing=True,
            misfire_grace_time=60,
            coalesce=True,
        )


async def _fire_agent(agent_id: str) -> None:
    try:
        await execute_agent(agent_id=agent_id, trigger="cron", trigger_payload={})
    except Exception as exc:
        log.exception("scheduled agent %s failed: %s", agent_id, exc)


_singleton: Scheduler | None = None


def get_scheduler() -> Scheduler:
    global _singleton
    if _singleton is None:
        _singleton = Scheduler()
    return _singleton


def next_fire_time(cron: str) -> str | None:
    """Return ISO-8601 timestamp of the next firing for a cron string,
    or None if the expression is invalid. Pure function — no scheduler
    state required, useful for the /schedule endpoint."""
    from datetime import datetime, timezone

    try:
        trigger = CronTrigger.from_crontab(cron)
    except Exception:
        return None
    nxt = trigger.get_next_fire_time(None, datetime.now(timezone.utc))
    if nxt is None:
        return None
    return nxt.isoformat()


async def run_forever() -> None:
    """Standalone scheduler loop (used by `pilothouse schedule run`)."""
    from .connectors import register_builtin_connectors
    from .db import init_db
    from .templates import register_builtin_templates

    register_builtin_connectors()
    register_builtin_templates()
    await init_db()
    sched = get_scheduler()
    await sched.start()
    log.info("Pilothouse scheduler running. Ctrl-C to stop.")
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await sched.stop()
