"""Multi-tenant isolation tests.

Each test exercises the full HTTP middleware so we test the same code
path real callers use: tenant resolution → query scoping → cross-tenant
access yields 404 (not 403, to avoid leaking existence).
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from pilothouse.api.server import build_app
from pilothouse.tenants import (
    add_api_key,
    create_tenant,
    delete_tenant,
    ensure_default_tenant,
)


@pytest.fixture
async def app_client():
    app = build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_default_tenant_exists_after_init() -> None:
    tid = await ensure_default_tenant()
    assert tid


async def test_two_tenants_cannot_see_each_others_agents(app_client: AsyncClient) -> None:
    await create_tenant("tenant-a", "Tenant A")
    await create_tenant("tenant-b", "Tenant B")
    await add_api_key("tenant-a", "key-aaa")
    await add_api_key("tenant-b", "key-bbb")

    # Tenant A creates an agent.
    r = await app_client.post(
        "/agents",
        json={"name": "secret-a", "template": "datadog_alert_triage", "params": {"service": "x"}},
        headers={"x-api-key": "key-aaa"},
    )
    assert r.status_code == 201, r.text
    agent_a_id = r.json()["id"]

    # Tenant B can't see it in the list.
    r = await app_client.get("/agents", headers={"x-api-key": "key-bbb"})
    assert r.status_code == 200
    names = [a["name"] for a in r.json()]
    assert "secret-a" not in names

    # Tenant B can't fetch by id either — 404 (not 403, to avoid info leak).
    r = await app_client.get(f"/agents/{agent_a_id}", headers={"x-api-key": "key-bbb"})
    assert r.status_code == 404

    # Tenant A can see and fetch its own.
    r = await app_client.get("/agents", headers={"x-api-key": "key-aaa"})
    assert "secret-a" in [a["name"] for a in r.json()]


async def test_same_agent_name_allowed_across_tenants(app_client: AsyncClient) -> None:
    await create_tenant("tenant-a", "Tenant A")
    await create_tenant("tenant-b", "Tenant B")
    await add_api_key("tenant-a", "key-aaa")
    await add_api_key("tenant-b", "key-bbb")
    body = {"name": "checkout-triage", "template": "datadog_alert_triage"}
    r1 = await app_client.post("/agents", json=body, headers={"x-api-key": "key-aaa"})
    r2 = await app_client.post("/agents", json=body, headers={"x-api-key": "key-bbb"})
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["id"] != r2.json()["id"]


async def test_invalid_api_key_rejected_when_keys_configured(app_client: AsyncClient) -> None:
    await create_tenant("tenant-a", "Tenant A")
    await add_api_key("tenant-a", "key-good")
    r = await app_client.get("/agents", headers={"x-api-key": "key-bad"})
    assert r.status_code == 401


async def test_anonymous_works_when_no_keys_anywhere(app_client: AsyncClient) -> None:
    # No tenant has any keys → routes everything to default tenant.
    r = await app_client.get("/agents")
    assert r.status_code == 200


async def test_me_endpoint_returns_resolved_tenant(app_client: AsyncClient) -> None:
    await create_tenant("tenant-a", "Tenant A")
    await add_api_key("tenant-a", "key-aaa")
    r = await app_client.get("/me", headers={"x-api-key": "key-aaa"})
    assert r.status_code == 200
    body = r.json()
    assert body["tenant_name"] == "tenant-a"


async def test_cannot_delete_default_tenant() -> None:
    with pytest.raises(ValueError):
        await delete_tenant("default")


async def test_runs_and_approvals_inherit_tenant(app_client: AsyncClient) -> None:
    """End-to-end: create agent in tenant-a, trigger run, verify the
    Approval row that gets created carries tenant-a, and tenant-b
    cannot resolve it."""
    await create_tenant("tenant-a")
    await create_tenant("tenant-b")
    await add_api_key("tenant-a", "ka")
    await add_api_key("tenant-b", "kb")

    r = await app_client.post(
        "/agents",
        json={
            "name": "scanner-x",
            "template": "pr_security_scanner",
            "params": {"repo": "acme/api", "auto_comment": True},
            "dry_run": False,
        },
        headers={"x-api-key": "ka"},
    )
    aid = r.json()["id"]

    r = await app_client.post(
        f"/agents/{aid}/trigger",
        json={"payload": {"pull_request": {"number": 1}, "repository": {"full_name": "acme/api"}}},
        headers={"x-api-key": "ka"},
    )
    run_id = r.json()["id"]
    assert r.json()["status"] == "awaiting_approval"

    # Tenant A sees its approvals.
    r = await app_client.get("/approvals?status=pending", headers={"x-api-key": "ka"})
    assert r.status_code == 200
    a_approvals = r.json()
    assert len(a_approvals) == 1
    approval_id = a_approvals[0]["id"]

    # Tenant B sees none, and 404s on direct fetch.
    r = await app_client.get("/approvals?status=pending", headers={"x-api-key": "kb"})
    assert r.json() == []
    r = await app_client.get(f"/approvals/{approval_id}", headers={"x-api-key": "kb"})
    assert r.status_code == 404

    # Tenant B can't resolve tenant A's approval.
    r = await app_client.post(
        f"/approvals/{approval_id}/resolve",
        json={"decision": "approve", "resolved_by": "attacker"},
        headers={"x-api-key": "kb"},
    )
    assert r.status_code == 404
