"""Rate-limit + dedup tests via the HTTP layer."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from pilothouse.api.server import build_app
from pilothouse.config import get_settings
from pilothouse.limits import (
    check_dedup,
    check_rate_limit,
    payload_digest,
    record_dedup,
    reset,
)


@pytest.fixture(autouse=True)
def _reset_limits():
    reset()
    yield
    reset()


@pytest.fixture
async def client():
    app = build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# --- unit ---------------------------------------------------------------


def test_payload_digest_is_stable_under_key_reorder() -> None:
    a = {"x": 1, "nested": {"a": 1, "b": 2}}
    b = {"nested": {"b": 2, "a": 1}, "x": 1}
    assert payload_digest("aid", a) == payload_digest("aid", b)


def test_payload_digest_changes_when_agent_changes() -> None:
    p = {"x": 1}
    assert payload_digest("aid-1", p) != payload_digest("aid-2", p)


async def test_rate_limit_allows_under_cap_and_blocks_over() -> None:
    for _ in range(3):
        assert await check_rate_limit("t1", limit_per_minute=3) is True
    assert await check_rate_limit("t1", limit_per_minute=3) is False
    # A different tenant has its own counter.
    assert await check_rate_limit("t2", limit_per_minute=3) is True


async def test_rate_limit_zero_disables() -> None:
    for _ in range(100):
        assert await check_rate_limit("t1", limit_per_minute=0) is True


async def test_dedup_returns_existing_run_id_in_window() -> None:
    payload = {"alert_id": "abc"}
    is_dup, _ = await check_dedup("t", "agent-1", payload, window_seconds=60)
    assert is_dup is False
    await record_dedup("t", "agent-1", payload, "run-A", window_seconds=60)
    is_dup, run_id = await check_dedup("t", "agent-1", payload, window_seconds=60)
    assert is_dup and run_id == "run-A"


async def test_dedup_isolated_per_tenant() -> None:
    payload = {"alert_id": "abc"}
    await record_dedup("t1", "agent-1", payload, "run-A", window_seconds=60)
    is_dup, _ = await check_dedup("t2", "agent-1", payload, window_seconds=60)
    assert is_dup is False


# --- HTTP integration ---------------------------------------------------


async def _create_agent(client: AsyncClient) -> str:
    r = await client.post(
        "/agents",
        json={
            "name": "limit-tester",
            "template": "datadog_alert_triage",
            "params": {"service": "checkout"},
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_dedup_via_manual_trigger_returns_same_run_id(client: AsyncClient) -> None:
    aid = await _create_agent(client)
    payload = {"alert_id": "dup-1"}
    r1 = await client.post(f"/agents/{aid}/trigger", json={"payload": payload})
    r2 = await client.post(f"/agents/{aid}/trigger", json={"payload": payload})
    assert r1.json()["id"] == r2.json()["id"]


async def test_rate_limit_returns_429(client: AsyncClient, monkeypatch) -> None:
    monkeypatch.setenv("PILOTHOUSE_RATE_LIMIT_PER_MINUTE", "2")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    aid = await _create_agent(client)
    # Different payloads so dedup doesn't kick in.
    statuses = []
    for i in range(5):
        r = await client.post(
            f"/agents/{aid}/trigger", json={"payload": {"i": i}}
        )
        statuses.append(r.status_code)
    # First 2 succeed; 3rd onward → 429.
    assert statuses[:2] == [200, 200]
    assert any(s == 429 for s in statuses[2:])
    monkeypatch.delenv("PILOTHOUSE_RATE_LIMIT_PER_MINUTE", raising=False)
    get_settings.cache_clear()  # type: ignore[attr-defined]
