"""Global /runs search + /schedule endpoint tests."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from pilothouse.api.server import build_app
from pilothouse.db import session
from pilothouse.models import Agent
from pilothouse.orchestration.executor import execute_agent


@pytest.fixture
async def client():
    app = build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _agent(name: str, template: str = "datadog_alert_triage", **kwargs) -> str:
    async with session() as s:
        a = Agent(name=name, template=template, params={"service": "x"}, **kwargs)
        s.add(a)
        await s.flush()
        return a.id


# --- /runs search -------------------------------------------------------


async def test_search_runs_filters_by_agent(client: AsyncClient) -> None:
    a1 = await _agent("agent-A")
    a2 = await _agent("agent-B")
    await execute_agent(agent_id=a1, trigger="manual", trigger_payload={"alert_id": "1"})
    await execute_agent(agent_id=a2, trigger="manual", trigger_payload={"alert_id": "2"})
    await execute_agent(agent_id=a2, trigger="manual", trigger_payload={"alert_id": "3"})

    r = await client.get("/runs?agent=agent-A")
    assert r.status_code == 200
    runs = r.json()
    assert len(runs) == 1


async def test_search_runs_filters_by_status_and_summary(client: AsyncClient) -> None:
    a = await _agent("agent-S")
    await execute_agent(agent_id=a, trigger="manual", trigger_payload={"alert_id": "1"})

    r = await client.get("/runs?status=succeeded")
    assert r.status_code == 200
    assert all(run["status"] == "succeeded" for run in r.json())

    r = await client.get("/runs?q=Summary")
    assert r.status_code == 200
    # Mock plan summary always contains "Summary".
    assert len(r.json()) >= 1


async def test_search_runs_pagination(client: AsyncClient) -> None:
    a = await _agent("agent-P")
    for i in range(7):
        await execute_agent(agent_id=a, trigger="manual", trigger_payload={"i": i})

    r = await client.get("/runs?limit=3")
    page1 = r.json()
    assert len(page1) == 3
    r = await client.get("/runs?limit=3&offset=3")
    page2 = r.json()
    assert len(page2) == 3
    assert {p["id"] for p in page1}.isdisjoint({p["id"] for p in page2})


async def test_unknown_status_yields_400(client: AsyncClient) -> None:
    r = await client.get("/runs?status=quantum")
    assert r.status_code == 400


# --- /schedule ----------------------------------------------------------


async def test_schedule_lists_only_enabled_with_cron(client: AsyncClient) -> None:
    await _agent("schedule-A", schedule_cron="*/5 * * * *", enabled=True)
    await _agent("schedule-B", schedule_cron=None, enabled=True)
    await _agent("schedule-C", schedule_cron="0 * * * *", enabled=False)

    r = await client.get("/schedule")
    assert r.status_code == 200
    rows = r.json()
    names = [row["name"] for row in rows]
    assert names == ["schedule-A"]
    assert rows[0]["next_fire"]  # ISO-8601 timestamp present
