"""Async SQLAlchemy engine + session factory.

SQLite is the default for MVP; the URL is overridable via settings so the same
ORM models swap onto Postgres in production without code changes.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import get_settings


class Base(DeclarativeBase):
    pass


_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _init() -> None:
    global _engine, _session_factory
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(settings.database_url, echo=False, future=True)
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)


async def init_db() -> None:
    """Create tables + ensure default tenant + backfill. Idempotent."""
    _init()
    # Import models so they register with Base.metadata before create_all.
    from . import models  # noqa: F401

    assert _engine is not None
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Bootstrap the default tenant + backfill any pre-existing rows.
    # This is what makes the multi-tenant rollout drop-in compatible
    # with single-tenant databases.
    from .tenants import ensure_default_tenant

    await ensure_default_tenant()


@asynccontextmanager
async def session() -> AsyncIterator[AsyncSession]:
    _init()
    assert _session_factory is not None
    async with _session_factory() as s:
        try:
            yield s
            await s.commit()
        except Exception:
            await s.rollback()
            raise
