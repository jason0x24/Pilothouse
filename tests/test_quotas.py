"""Per-tenant quota enforcement tests (max_agents + max_runs_per_day)."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from pilothouse.api.server import build_app
from pilothouse.tenants import (
    add_api_key,
    create_tenant,
    set_quota,
)


@pytest.fixture
async def client():
    app = build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _setup_capped_tenant(name: str, key: str, *, max_agents: int = 0, max_runs: int = 0) -> None:
    await create_tenant(name)
    await add_api_key(name, key)
    await set_quota(name, max_agents=max_agents, max_runs_per_day=max_runs)


async def test_max_agents_enforced(client: AsyncClient) -> None:
    await _setup_capped_tenant("capped-a", "key-a", max_agents=2)

    body = lambda n: {"name": n, "template": "datadog_alert_triage", "params": {"service": "x"}}
    r1 = await client.post("/agents", json=body("a-1"), headers={"x-api-key": "key-a"})
    r2 = await client.post("/agents", json=body("a-2"), headers={"x-api-key": "key-a"})
    r3 = await client.post("/agents", json=body("a-3"), headers={"x-api-key": "key-a"})
    assert (r1.status_code, r2.status_code) == (201, 201)
    assert r3.status_code == 403
    assert "quota" in r3.text.lower()


async def test_max_runs_per_day_enforced(client: AsyncClient) -> None:
    await _setup_capped_tenant("capped-r", "key-r", max_runs=2)
    r = await client.post(
        "/agents",
        json={"name": "ag", "template": "datadog_alert_triage", "params": {"service": "x"}},
        headers={"x-api-key": "key-r"},
    )
    aid = r.json()["id"]

    # Different payloads to bypass dedup.
    statuses = []
    for i in range(4):
        r = await client.post(
            f"/agents/{aid}/trigger",
            json={"payload": {"i": i}},
            headers={"x-api-key": "key-r"},
        )
        statuses.append(r.status_code)
    # Expect: first two succeed, then 429 (run quota).
    assert statuses[:2] == [200, 200]
    assert any(s == 429 for s in statuses[2:])


async def test_quota_zero_means_unlimited(client: AsyncClient) -> None:
    """Default tenant has quota=0 → no cap."""
    body = lambda n: {"name": n, "template": "datadog_alert_triage", "params": {"service": "x"}}
    for i in range(5):
        r = await client.post("/agents", json=body(f"unlimited-{i}"))
        assert r.status_code == 201
