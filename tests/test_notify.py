"""Approval notification listener — verify outbound webhook fires on
`approval_requested`. We don't test live Slack here; we point the
generic webhook URL at a captured httpx mock transport via monkeypatch.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from pilothouse.db import session
from pilothouse.events import get_bus, reset_bus
from pilothouse.models import Agent
from pilothouse.notify import start_notifier, stop_notifier
from pilothouse.orchestration.executor import execute_agent


async def test_generic_webhook_called_on_approval_requested(monkeypatch) -> None:
    captured: list[dict[str, Any]] = []

    async def fake_post(self, url, *, json=None, **kw):  # type: ignore[no-redef]
        captured.append({"url": url, "json": json})
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setenv("PILOTHOUSE_NOTIFY_WEBHOOK_URL", "https://hook.example.invalid/incoming")

    reset_bus()
    start_notifier()
    try:
        # Trigger a run that pauses (non-dry-run scanner with auto_comment).
        async with session() as s:
            a = Agent(
                name="notify-test",
                template="pr_security_scanner",
                params={"repo": "acme/api", "auto_comment": True},
                dry_run=False,
            )
            s.add(a)
            await s.flush()
            aid = a.id

        await execute_agent(
            agent_id=aid,
            trigger="manual",
            trigger_payload={
                "pull_request": {"number": 5},
                "repository": {"full_name": "acme/api"},
            },
        )
        # The notifier runs in a background task; give it a moment.
        for _ in range(50):
            if captured:
                break
            await asyncio.sleep(0.02)
    finally:
        stop_notifier()

    assert captured, "expected outbound webhook to fire"
    body = captured[0]["json"]
    assert body["kind"] == "approval_requested"
    assert body["agent_name"] == "notify-test"
    assert body["tool"] == "github_post_pr_comment"
