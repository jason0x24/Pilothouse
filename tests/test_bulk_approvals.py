"""Bulk approve/reject endpoint tests."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from pilothouse.api.server import build_app
from pilothouse.db import session
from pilothouse.models import Agent, Approval, ApprovalStatus, Run, RunStatus
from pilothouse.orchestration.executor import execute_agent


@pytest.fixture
async def client():
    app = build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _scanner_with_pause(name: str, pr_number: int) -> str:
    """Create a non-dry-run scanner agent and trigger one paused run.
    Returns the run id."""
    async with session() as s:
        a = Agent(
            name=name,
            template="pr_security_scanner",
            params={"repo": "acme/api", "auto_comment": True},
            dry_run=False,
        )
        s.add(a)
        await s.flush()
        aid = a.id
    return await execute_agent(
        agent_id=aid,
        trigger="manual",
        trigger_payload={
            "pull_request": {"number": pr_number},
            "repository": {"full_name": "acme/api"},
        },
    )


async def test_bulk_approve_by_ids_resumes_runs(client: AsyncClient) -> None:
    rid1 = await _scanner_with_pause("bulk-1", 1)
    rid2 = await _scanner_with_pause("bulk-2", 2)
    async with session() as s:
        ids = [
            a.id
            for a in (await s.execute(select(Approval))).scalars().all()
        ]
    assert len(ids) == 2

    r = await client.post(
        "/approvals/resolve-batch",
        json={"decision": "approve", "ids": ids, "resolved_by": "bulk-tester"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["count"] == 2
    assert all(item["ok"] for item in data["resolved"])

    async with session() as s:
        runs = (await s.execute(select(Run))).scalars().all()
    statuses = sorted(
        r.status.value if hasattr(r.status, "value") else str(r.status) for r in runs
    )
    assert statuses == ["succeeded", "succeeded"]


async def test_bulk_reject_by_filter_only_targets_pending(client: AsyncClient) -> None:
    rid1 = await _scanner_with_pause("filter-1", 11)
    rid2 = await _scanner_with_pause("filter-2", 12)

    # Resolve one manually first so we can verify the filter only acts on pending.
    async with session() as s:
        first = (await s.execute(select(Approval))).scalars().first()
        first_id = first.id
    await client.post(
        f"/approvals/{first_id}/resolve",
        json={"decision": "approve", "resolved_by": "manual"},
    )

    # Now bulk-reject everything pending matching the tool filter.
    r = await client.post(
        "/approvals/resolve-batch",
        json={
            "decision": "reject",
            "filters": {"tool": "github_post_pr_comment"},
            "resolved_by": "bulk",
            "reason": "rotate first",
        },
    )
    assert r.status_code == 200
    # Exactly one approval was still pending → exactly one resolved.
    assert r.json()["count"] == 1

    async with session() as s:
        approvals = (await s.execute(select(Approval))).scalars().all()
    decisions = sorted(
        a.status.value if hasattr(a.status, "value") else str(a.status) for a in approvals
    )
    assert decisions == ["approved", "rejected"]


async def test_bulk_with_already_resolved_id_reports_per_item_status(
    client: AsyncClient,
) -> None:
    rid = await _scanner_with_pause("already", 9)
    async with session() as s:
        ap = (await s.execute(select(Approval))).scalars().one()
        ap_id = ap.id
    await client.post(
        f"/approvals/{ap_id}/resolve",
        json={"decision": "approve", "resolved_by": "first"},
    )
    # Now try to approve it again in a batch.
    r = await client.post(
        "/approvals/resolve-batch",
        json={"decision": "approve", "ids": [ap_id]},
    )
    data = r.json()
    assert data["count"] == 0
    assert data["resolved"][0]["ok"] is False
    assert "already" in data["resolved"][0]["error"]
