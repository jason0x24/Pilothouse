"""SSE event-stream tests.

Uses httpx ASGITransport so we don't depend on a running server. We
trigger a run synchronously (mock mode → finishes immediately) and then
hit the stream endpoint; since the run is terminal the stream replays
history and closes.
"""

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
    async with AsyncClient(transport=transport, base_url="http://test", timeout=10) as c:
        yield c


async def _make_run() -> str:
    async with session() as s:
        a = Agent(name="sse-agent", template="datadog_alert_triage", params={"service": "checkout"})
        s.add(a)
        await s.flush()
        aid = a.id
    return await execute_agent(agent_id=aid, trigger="manual", trigger_payload={"alert_id": "x"})


async def test_stream_replays_history_for_terminal_run(client: AsyncClient) -> None:
    run_id = await _make_run()
    async with client.stream("GET", f"/runs/{run_id}/events/stream") as resp:
        assert resp.status_code == 200
        body_chunks: list[str] = []
        async for chunk in resp.aiter_text():
            body_chunks.append(chunk)
            if "event: end" in "".join(body_chunks):
                break
    body = "".join(body_chunks)
    # Run is succeeded so we should see at least the start + finish events.
    assert "event: run_started" in body
    assert "event: run_finished" in body
    assert "event: end" in body


async def test_stream_404s_for_unknown_run(client: AsyncClient) -> None:
    async with client.stream("GET", "/runs/does-not-exist/events/stream") as resp:
        body = ""
        async for chunk in resp.aiter_text():
            body += chunk
            if "event: error" in body:
                break
    assert "run not found" in body
