"""Failure-path notification: a run_terminal event with status=failed
fires the same generic webhook used by approval_requested."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from pilothouse.config import get_settings
from pilothouse.db import session
from pilothouse.events import reset_bus
from pilothouse.models import Agent
from pilothouse.notify import start_notifier, stop_notifier
from pilothouse.orchestration.executor import execute_agent


async def test_failure_fires_generic_webhook(monkeypatch) -> None:
    captured: list[dict[str, Any]] = []

    async def fake_post(self, url, *, json=None, **kw):
        captured.append({"url": url, "json": json})
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setenv("PILOTHOUSE_NOTIFY_WEBHOOK_URL", "https://hook.example/x")

    # Force the runtime into a failed state by capping iterations to 0.
    # Re-cache the settings so the change is visible.
    monkeypatch.setenv("PILOTHOUSE_MAX_TOOL_ITERATIONS", "0")
    get_settings.cache_clear()  # type: ignore[attr-defined]

    reset_bus()
    start_notifier()
    try:
        async with session() as s:
            a = Agent(
                name="fail-notify-test",
                template="datadog_alert_triage",
                params={"service": "checkout"},
            )
            s.add(a)
            await s.flush()
            aid = a.id

        await execute_agent(
            agent_id=aid,
            trigger="manual",
            trigger_payload={"alert_id": "x"},
        )

        for _ in range(50):
            if any(c["json"].get("kind") == "run_failure" for c in captured):
                break
            await asyncio.sleep(0.02)
    finally:
        stop_notifier()
        monkeypatch.delenv("PILOTHOUSE_MAX_TOOL_ITERATIONS", raising=False)
        get_settings.cache_clear()  # type: ignore[attr-defined]

    failure_calls = [c for c in captured if c["json"].get("kind") == "run_failure"]
    assert failure_calls, "expected a run_failure notification"
    body = failure_calls[0]["json"]
    assert body["status"] == "failed"
    assert body["agent_name"] == "fail-notify-test"
