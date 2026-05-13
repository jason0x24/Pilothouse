"""Run audit export — JSON + CSV."""

from __future__ import annotations

import csv
import io
import json

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


async def _seed_run() -> str:
    async with session() as s:
        a = Agent(name="export-test", template="datadog_alert_triage", params={"service": "x"})
        s.add(a)
        await s.flush()
        aid = a.id
    return await execute_agent(
        agent_id=aid, trigger="manual", trigger_payload={"alert_id": "abc"}
    )


async def test_json_export_carries_full_audit(client: AsyncClient) -> None:
    run_id = await _seed_run()
    r = await client.get(f"/runs/{run_id}/export.json")
    assert r.status_code == 200
    assert r.headers["content-disposition"].startswith("attachment;")
    bundle = r.json()
    assert set(bundle.keys()) >= {"run", "agent_snapshot", "events", "approvals"}
    assert bundle["run"]["id"] == run_id
    assert bundle["agent_snapshot"]["name"] == "export-test"
    assert len(bundle["events"]) > 0
    # The export must include both the start and the end events.
    kinds = [e["kind"] for e in bundle["events"]]
    assert "run_started" in kinds
    assert "run_finished" in kinds


async def test_csv_export_one_row_per_event(client: AsyncClient) -> None:
    run_id = await _seed_run()
    r = await client.get(f"/runs/{run_id}/export.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(r.text)))
    # Header + one row per event.
    assert rows[0] == ["created_at", "kind", "data_json"]
    assert len(rows) > 1
    # JSON column must be parseable.
    for row in rows[1:]:
        json.loads(row[2])


async def test_export_404_for_other_tenant(client: AsyncClient) -> None:
    """Cross-tenant export must 404 just like other reads."""
    from pilothouse.tenants import add_api_key, create_tenant

    await create_tenant("attacker")
    await add_api_key("attacker", "key-attack")
    run_id = await _seed_run()  # owned by default tenant

    r = await client.get(
        f"/runs/{run_id}/export.json", headers={"x-api-key": "key-attack"}
    )
    assert r.status_code == 404
