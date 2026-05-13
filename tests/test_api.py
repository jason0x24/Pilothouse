"""HTTP API smoke tests via FastAPI's TestClient."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
async def client():
    # Skip scheduler startup during tests by importing build_app *without*
    # going through lifespan; the TestClient won't invoke lifespan unless
    # asked, and our manual lifespan management here keeps tests fast.
    from pilothouse.api.server import build_app

    app = build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_healthz(client: AsyncClient) -> None:
    r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


async def test_templates_listed(client: AsyncClient) -> None:
    r = await client.get("/templates")
    assert r.status_code == 200
    keys = {t["key"] for t in r.json()}
    assert {"datadog_alert_triage", "pr_security_scanner", "k8s_pod_investigator"}.issubset(keys)


async def test_create_and_trigger_agent(client: AsyncClient) -> None:
    r = await client.post(
        "/agents",
        json={
            "name": "api-triage",
            "template": "datadog_alert_triage",
            "params": {"service": "checkout"},
        },
    )
    assert r.status_code == 201, r.text
    agent = r.json()
    aid = agent["id"]

    r2 = await client.post(
        f"/agents/{aid}/trigger",
        json={"payload": {"alert_id": "abc"}, "dry_run": True},
    )
    assert r2.status_code == 200, r2.text
    run = r2.json()
    assert run["status"] == "succeeded"
    assert "Summary" in run["summary"]

    r3 = await client.get(f"/runs/{run['id']}/events")
    assert r3.status_code == 200
    events = r3.json()
    assert any(e["kind"] == "tool_call" for e in events)


async def test_unknown_template_rejected(client: AsyncClient) -> None:
    r = await client.post(
        "/agents", json={"name": "bad", "template": "does-not-exist"}
    )
    assert r.status_code == 400
