"""Shared pytest fixtures. Each test gets a fresh in-memory SQLite DB."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# Point Pilothouse at an isolated DB *before* importing anything from it.
_TMPDIR = tempfile.mkdtemp(prefix="pilothouse-tests-")
os.environ["PILOTHOUSE_DATA_DIR"] = _TMPDIR
os.environ["PILOTHOUSE_DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMPDIR}/test.db"
# Force mock mode regardless of operator-shell env vars.
for _key in (
    "PILOTHOUSE_ANTHROPIC_API_KEY",
    "PILOTHOUSE_OPENROUTER_API_KEY",
    "PILOTHOUSE_OPENAI_API_KEY",
    "PILOTHOUSE_MODEL_PROVIDER",
):
    os.environ.pop(_key, None)

from pilothouse.connectors import register_builtin_connectors  # noqa: E402
from pilothouse.db import init_db  # noqa: E402
from pilothouse.templates import register_builtin_templates  # noqa: E402


@pytest.fixture(autouse=True)
async def _setup_db():
    # Recreate the DB file for each test so state doesn't leak.
    db_path = Path(_TMPDIR) / "test.db"
    if db_path.exists():
        db_path.unlink()
    # Reset the cached engine so it re-opens against the fresh file.
    import pilothouse.db as dbmod

    dbmod._engine = None
    dbmod._session_factory = None

    register_builtin_connectors()
    register_builtin_templates()
    # Reset cached default tenant id; each test starts with a fresh DB so
    # the cache from the previous test is stale.
    from pilothouse.tenants import reset_default_tenant_cache

    reset_default_tenant_cache()
    # Also reset the cached env-derived settings (api_keys etc.) — tests
    # often monkeypatch env vars and expect them to take effect.
    from pilothouse.config import get_settings as _gs

    _gs.cache_clear()  # type: ignore[attr-defined]
    await init_db()
    yield
