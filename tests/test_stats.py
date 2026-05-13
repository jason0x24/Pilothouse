"""Stats endpoint test — drives runs then asserts aggregation shape."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from pilothouse.db import session
from pilothouse.models import Agent
from pilothouse.orchestration.executor import execute_agent


@pytest.fixture
async def client():
    from pilothouse.api.server import build_app

    app = build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_stats_returns_aggregated_window(client: AsyncClient) -> None:
    # Drive two runs across two agents so the by_agent split is non-trivial.
    async with session() as s:
        a1 = Agent(name="stats-1", template="datadog_alert_triage", params={"service": "checkout"})
        a2 = Agent(name="stats-2", template="datadog_alert_triage", params={"service": "orders"})
        s.add(a1)
        s.add(a2)
        await s.flush()
        aid1, aid2 = a1.id, a2.id

    for aid in (aid1, aid1, aid2):
        await execute_agent(agent_id=aid, trigger="manual", trigger_payload={"alert_id": "x"})

    r = await client.get("/stats?days=1")
    assert r.status_code == 200
    data = r.json()
    assert data["window_days"] == 1
    assert data["totals"]["runs"] == 3
    assert data["totals"]["tokens_in"] > 0
    # by_agent has both agents, with stats-1 having more runs.
    agent_names = [row["agent"] for row in data["by_agent"]]
    assert {"stats-1", "stats-2"}.issubset(set(agent_names))
    s1 = next(row for row in data["by_agent"] if row["agent"] == "stats-1")
    s2 = next(row for row in data["by_agent"] if row["agent"] == "stats-2")
    assert s1["runs"] == 2
    assert s2["runs"] == 1
    # by_day always has exactly `days` entries (zero-filled).
    assert len(data["by_day"]) == 1
    assert data["by_status"].get("succeeded", 0) == 3
