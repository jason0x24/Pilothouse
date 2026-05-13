"""Cancel + TTL + bus + metrics tests."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from pilothouse.config import get_settings
from pilothouse.db import session
from pilothouse.events import get_bus, reset_bus
from pilothouse.models import (
    Agent,
    Approval,
    ApprovalStatus,
    Event,
    EventKind,
    Run,
    RunStatus,
)
from pilothouse.orchestration.executor import (
    cancel_run,
    execute_agent,
    sweep_expired_approvals,
)


async def _create_agent(name: str, template: str, params: dict, *, dry_run: bool = True) -> str:
    async with session() as s:
        a = Agent(name=name, template=template, params=params, dry_run=dry_run)
        s.add(a)
        await s.flush()
        return a.id


# --- cancel ---------------------------------------------------------------


async def test_cancel_awaiting_approval_rejects_pending_and_marks_cancelled() -> None:
    aid = await _create_agent(
        "scanner-cancel",
        "pr_security_scanner",
        {"repo": "acme/api", "auto_comment": True},
        dry_run=False,
    )
    run_id = await execute_agent(
        agent_id=aid,
        trigger="manual",
        trigger_payload={"pull_request": {"number": 5}, "repository": {"full_name": "acme/api"}},
    )
    async with session() as s:
        run = (await s.execute(select(Run).where(Run.id == run_id))).scalar_one()
        assert run.status == RunStatus.awaiting_approval

    await cancel_run(run_id, by="tester")
    async with session() as s:
        run = (await s.execute(select(Run).where(Run.id == run_id))).scalar_one()
        approvals = (
            await s.execute(select(Approval).where(Approval.run_id == run_id))
        ).scalars().all()
        events = (
            await s.execute(select(Event).where(Event.run_id == run_id))
        ).scalars().all()
    assert run.status == RunStatus.cancelled
    assert run.finished_at is not None
    assert all(a.status == ApprovalStatus.rejected for a in approvals)
    assert any(e.kind == EventKind.run_cancelled for e in events)


async def test_cancel_is_idempotent_on_terminal_run() -> None:
    aid = await _create_agent("triage-cancel", "datadog_alert_triage", {"service": "checkout"})
    run_id = await execute_agent(
        agent_id=aid, trigger="manual", trigger_payload={"alert_id": "x"}
    )
    async with session() as s:
        run = (await s.execute(select(Run).where(Run.id == run_id))).scalar_one()
    assert run.status == RunStatus.succeeded
    # cancelling a succeeded run should be a no-op (no exception).
    await cancel_run(run_id, by="tester")
    async with session() as s:
        run = (await s.execute(select(Run).where(Run.id == run_id))).scalar_one()
    assert run.status == RunStatus.succeeded


# --- approval TTL ---------------------------------------------------------


async def test_expired_approval_resumes_run_with_rejection() -> None:
    aid = await _create_agent(
        "scanner-ttl",
        "pr_security_scanner",
        {"repo": "acme/api", "auto_comment": True},
        dry_run=False,
    )
    run_id = await execute_agent(
        agent_id=aid,
        trigger="manual",
        trigger_payload={"pull_request": {"number": 19}, "repository": {"full_name": "acme/api"}},
    )
    async with session() as s:
        ap = (
            await s.execute(select(Approval).where(Approval.run_id == run_id))
        ).scalars().one()
        # Backdate so it counts as expired against the configured TTL.
        ap.created_at = datetime.now(timezone.utc) - timedelta(
            minutes=get_settings().approval_ttl_minutes + 5
        )

    expired = await sweep_expired_approvals()
    assert expired == 1

    async with session() as s:
        ap = (
            await s.execute(select(Approval).where(Approval.run_id == run_id))
        ).scalars().one()
        run = (await s.execute(select(Run).where(Run.id == run_id))).scalar_one()
        events = (
            await s.execute(select(Event).where(Event.run_id == run_id))
        ).scalars().all()
    assert ap.status == ApprovalStatus.rejected
    assert "expired" in ap.rejection_reason
    assert run.status == RunStatus.succeeded
    assert any(e.kind == EventKind.approval_expired for e in events)


# --- event bus ------------------------------------------------------------


async def test_bus_publishes_every_emitted_event() -> None:
    reset_bus()
    aid = await _create_agent("bus-test", "datadog_alert_triage", {"service": "checkout"})

    bus = get_bus()
    received: list[str] = []

    async def consume() -> None:
        async for ev in bus.subscribe("*"):
            received.append(ev.kind)
            if ev.kind in {"run_finished", "run_cancelled"}:
                return

    task = asyncio.create_task(consume())
    await execute_agent(agent_id=aid, trigger="manual", trigger_payload={"alert_id": "y"})
    # Give consumer a moment to drain — bus is in-memory but cooperative.
    await asyncio.wait_for(task, timeout=2)

    assert "run_started" in received
    assert "tool_call" in received
    assert "run_finished" in received


# --- metrics + cancel via HTTP -------------------------------------------


@pytest.fixture
async def client():
    from pilothouse.api.server import build_app

    app = build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_metrics_endpoint_exposes_counters(client: AsyncClient) -> None:
    # Drive one run so counters are non-zero.
    aid = await _create_agent("metrics-test", "datadog_alert_triage", {"service": "checkout"})
    await execute_agent(agent_id=aid, trigger="manual", trigger_payload={"alert_id": "z"})

    r = await client.get("/metrics")
    assert r.status_code == 200
    body = r.text
    assert "pilothouse_events_total" in body
    assert "pilothouse_tool_invocations_total" in body
    assert "pilothouse_run_status_total" in body
    assert "pilothouse_agents" in body


async def test_cancel_via_http(client: AsyncClient) -> None:
    # Create a non-dry-run scanner so it pauses for approval.
    r = await client.post(
        "/agents",
        json={
            "name": "scanner-http-cancel",
            "template": "pr_security_scanner",
            "params": {"repo": "acme/api", "auto_comment": True},
            "dry_run": False,
        },
    )
    assert r.status_code == 201, r.text
    aid = r.json()["id"]

    trigger = await client.post(
        f"/agents/{aid}/trigger",
        json={"payload": {"pull_request": {"number": 4}, "repository": {"full_name": "acme/api"}}},
    )
    run_id = trigger.json()["id"]
    assert trigger.json()["status"] == "awaiting_approval"

    cancel = await client.post(f"/runs/{run_id}/cancel", json={"by": "http-tester"})
    assert cancel.status_code == 200
    assert cancel.json()["status"] == "cancelled"


# --- API key auth --------------------------------------------------------


async def test_api_keys_required_when_configured(monkeypatch) -> None:
    from pilothouse.api.server import build_app
    from pilothouse.config import get_settings

    monkeypatch.setenv("PILOTHOUSE_API_KEYS", "k1, k2")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        app = build_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            # Public endpoints unaffected.
            assert (await c.get("/healthz")).status_code == 200
            assert (await c.get("/metrics")).status_code == 200
            # Protected endpoint rejects without key.
            unauthed = await c.get("/templates")
            assert unauthed.status_code == 401
            # And accepts with a valid key.
            ok = await c.get("/templates", headers={"X-API-Key": "k2"})
            assert ok.status_code == 200
    finally:
        monkeypatch.delenv("PILOTHOUSE_API_KEYS", raising=False)
        get_settings.cache_clear()  # type: ignore[attr-defined]
