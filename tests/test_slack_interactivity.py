"""Slack interactivity webhook — button clicks resolve the approval."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import httpx
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


async def _scanner_paused() -> str:
    """Trigger a non-dry-run PR scanner so it pauses for approval."""
    async with session() as s:
        a = Agent(
            name="slack-int-scanner",
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
            "pull_request": {"number": 42},
            "repository": {"full_name": "acme/api"},
        },
    )


def _slack_post(approval_id: str, action_id: str = "approve") -> tuple[bytes, dict]:
    """Build a Slack-style interactivity POST body + headers."""
    payload = {
        "type": "block_actions",
        "user": {"username": "alice"},
        "channel": {"id": "C123"},
        "message": {"ts": "1700000000.000100"},
        "actions": [{"action_id": action_id, "value": approval_id}],
    }
    body = urlencode({"payload": json.dumps(payload)}).encode()
    return body, {}


async def test_slack_button_approve_resolves_and_resumes(
    client: AsyncClient, monkeypatch
) -> None:
    # Patch httpx so OUTBOUND slack.com calls are captured, but the
    # test client's POST (which also uses httpx.AsyncClient.post under
    # the hood) still reaches the app via ASGITransport. We delegate to
    # the original `post` for everything that isn't slack.com.
    captured: list[dict] = []
    original_post = httpx.AsyncClient.post

    async def fake_post(self, url, *args, **kw):
        if isinstance(url, str) and "slack.com" in url:
            captured.append({"url": url, "json": kw.get("json")})
            return httpx.Response(200, json={"ok": True})
        return await original_post(self, url, *args, **kw)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setenv("PILOTHOUSE_SLACK_BOT_TOKEN", "xoxb-test")
    # Settings are @lru_cache'd; clear so the new bot token is picked up.
    from pilothouse.config import get_settings as _gs

    _gs.cache_clear()  # type: ignore[attr-defined]

    run_id = await _scanner_paused()
    async with session() as s:
        ap = (await s.execute(select(Approval).where(Approval.run_id == run_id))).scalar_one()
        ap_id = ap.id

    body, _ = _slack_post(ap_id, action_id="approve")
    r = await client.post(
        "/webhooks/slack/interactivity",
        content=body,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 200, r.text

    async with session() as s:
        ap = (await s.execute(select(Approval).where(Approval.id == ap_id))).scalar_one()
        run = (await s.execute(select(Run).where(Run.id == run_id))).scalar_one()
    assert ap.status == ApprovalStatus.approved
    assert ap.resolved_by == "slack:alice"
    # Run should have resumed and finished.
    assert run.status == RunStatus.succeeded
    # And we should have called Slack chat.update.
    assert any("chat.update" in c["url"] for c in captured)


async def test_slack_button_reject_records_reason(
    client: AsyncClient, monkeypatch
) -> None:
    original_post = httpx.AsyncClient.post

    async def fake_post(self, url, *args, **kw):
        if isinstance(url, str) and "slack.com" in url:
            return httpx.Response(200, json={"ok": True})
        return await original_post(self, url, *args, **kw)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setenv("PILOTHOUSE_SLACK_BOT_TOKEN", "xoxb-test")
    # Settings are @lru_cache'd; clear so the new bot token is picked up.
    from pilothouse.config import get_settings as _gs

    _gs.cache_clear()  # type: ignore[attr-defined]

    run_id = await _scanner_paused()
    async with session() as s:
        ap_id = (
            await s.execute(select(Approval).where(Approval.run_id == run_id))
        ).scalar_one().id

    body, _ = _slack_post(ap_id, action_id="reject")
    r = await client.post(
        "/webhooks/slack/interactivity",
        content=body,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 200
    async with session() as s:
        ap = (await s.execute(select(Approval).where(Approval.id == ap_id))).scalar_one()
    assert ap.status == ApprovalStatus.rejected
    assert "rejected from Slack" in ap.rejection_reason


async def test_slack_signature_required_when_secret_set(
    client: AsyncClient, monkeypatch
) -> None:
    monkeypatch.setenv("PILOTHOUSE_SLACK_SIGNING_SECRET", "ssecret")
    body, _ = _slack_post("any-id", action_id="approve")
    # No signature headers → 401.
    r = await client.post(
        "/webhooks/slack/interactivity",
        content=body,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 401

    # With valid signature it gets to the body parsing stage (404 because
    # the approval doesn't exist).
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(b"ssecret", f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
    r = await client.post(
        "/webhooks/slack/interactivity",
        content=body,
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "x-slack-request-timestamp": ts,
            "x-slack-signature": sig,
        },
    )
    assert r.status_code == 404
