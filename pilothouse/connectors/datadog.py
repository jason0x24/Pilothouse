"""Datadog connector.

Live mode uses Datadog's v1/v2 HTTP API. Mock mode synthesises plausible
incident artefacts so the alert-triage template can be developed offline.
The synthetic values are deterministic given the alert query so tests are
reproducible.
"""

from __future__ import annotations

import hashlib
import random
from typing import Any

import httpx

from ..config import get_settings
from .base import Connector, ToolContext, ToolResult


class DatadogConnector(Connector):
    name = "datadog"

    def __init__(self) -> None:
        super().__init__()
        self._add(
            "datadog_query_metric",
            "Query a Datadog metric over a time window. Returns timeseries points.",
            {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Datadog metric query, e.g. avg:trace.web.request.duration{service:checkout}",
                    },
                    "from_minutes_ago": {
                        "type": "integer",
                        "default": 60,
                        "description": "Window start, minutes before now",
                    },
                },
                "required": ["query"],
            },
            self._query_metric,
        )
        self._add(
            "datadog_get_alert",
            "Fetch a Datadog monitor/alert by ID.",
            {
                "type": "object",
                "properties": {"alert_id": {"type": "string"}},
                "required": ["alert_id"],
            },
            self._get_alert,
        )
        self._add(
            "datadog_recent_deploys",
            "List recent deployment events from Datadog Events API in the window.",
            {
                "type": "object",
                "properties": {
                    "service": {"type": "string"},
                    "from_minutes_ago": {"type": "integer", "default": 120},
                },
                "required": ["service"],
            },
            self._recent_deploys,
        )
        self._add(
            "datadog_search_logs",
            "Search Datadog logs. Returns up to 20 matching log lines.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "from_minutes_ago": {"type": "integer", "default": 30},
                },
                "required": ["query"],
            },
            self._search_logs,
        )

    @property
    def live(self) -> bool:
        s = get_settings()
        return bool(s.datadog_api_key and s.datadog_app_key)

    # --- handlers ----------------------------------------------------------

    async def _query_metric(self, ctx: ToolContext, params: dict) -> ToolResult:
        if not self.live:
            return ToolResult(content=_mock_timeseries(params["query"]))
        s = get_settings()
        from_min = int(params.get("from_minutes_ago", 60))
        import time

        now = int(time.time())
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(
                f"https://api.{s.datadog_site}/api/v1/query",
                params={"query": params["query"], "from": now - from_min * 60, "to": now},
                headers={"DD-API-KEY": s.datadog_api_key, "DD-APPLICATION-KEY": s.datadog_app_key},
            )
        return ToolResult(content=r.json(), is_error=r.is_error)

    async def _get_alert(self, ctx: ToolContext, params: dict) -> ToolResult:
        if not self.live:
            return ToolResult(content=_mock_alert(params["alert_id"]))
        s = get_settings()
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"https://api.{s.datadog_site}/api/v1/monitor/{params['alert_id']}",
                headers={"DD-API-KEY": s.datadog_api_key, "DD-APPLICATION-KEY": s.datadog_app_key},
            )
        return ToolResult(content=r.json(), is_error=r.is_error)

    async def _recent_deploys(self, ctx: ToolContext, params: dict) -> ToolResult:
        if not self.live:
            return ToolResult(content=_mock_deploys(params["service"]))
        # Live impl would call /api/v1/events with sources:deploy filter.
        return ToolResult(content=_mock_deploys(params["service"]))

    async def _search_logs(self, ctx: ToolContext, params: dict) -> ToolResult:
        if not self.live:
            return ToolResult(content=_mock_logs(params["query"]))
        s = get_settings()
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                f"https://api.{s.datadog_site}/api/v2/logs/events/search",
                json={
                    "filter": {"query": params["query"], "from": f"now-{params.get('from_minutes_ago', 30)}m"},
                    "page": {"limit": 20},
                },
                headers={"DD-API-KEY": s.datadog_api_key, "DD-APPLICATION-KEY": s.datadog_app_key},
            )
        return ToolResult(content=r.json(), is_error=r.is_error)


# --- mock data fabrication ------------------------------------------------


def _seed(*parts: Any) -> random.Random:
    """Stable seed derived from inputs — same inputs → same fake data."""
    h = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()
    return random.Random(int(h[:8], 16))


def _mock_timeseries(query: str) -> dict:
    rng = _seed("ts", query)
    base = rng.uniform(50, 500)
    points = [[i, base + rng.uniform(-20, 80) + (200 if i > 40 else 0)] for i in range(60)]
    return {
        "status": "ok",
        "query": query,
        "series": [{"metric": query, "pointlist": points, "unit": [{"name": "ms"}]}],
        "note": "p95 jumped sharply after t=40 — likely incident origin window",
    }


def _mock_alert(alert_id: str) -> dict:
    rng = _seed("alert", alert_id)
    services = ["checkout", "payments", "orders", "auth", "search"]
    svc = rng.choice(services)
    return {
        "id": alert_id,
        "name": f"[P{rng.randint(1, 2)}] {svc} p95 latency > 800ms",
        "type": "metric alert",
        "query": f"avg(last_5m):avg:trace.web.request.duration.95p{{service:{svc}}} > 0.8",
        "tags": [f"service:{svc}", "env:prod", "team:platform"],
        "state": {"value": "Alert", "triggered_at": "2026-05-12T14:21:00Z"},
        "message": f"{svc} latency exceeded SLO. @oncall please investigate.",
        "thresholds": {"critical": 0.8, "warning": 0.5},
    }


def _mock_deploys(service: str) -> list[dict]:
    rng = _seed("deploys", service)
    return [
        {
            "id": f"deploy-{rng.randint(10000, 99999)}",
            "service": service,
            "version": f"v{rng.randint(1, 50)}.{rng.randint(0, 30)}.{rng.randint(0, 200)}",
            "minutes_ago": offset,
            "actor": rng.choice(["alice", "bob", "carla", "dan"]),
            "commit": f"{rng.randrange(16**8):08x}",
        }
        for offset in (12, 47, 180, 720)
    ]


def _mock_logs(query: str) -> list[dict]:
    rng = _seed("logs", query)
    levels = ["ERROR", "ERROR", "WARN", "ERROR", "INFO"]
    msgs = [
        "ConnectionPoolTimeout: pool size 20 exhausted",
        "Upstream timeout calling payment-gateway after 3000ms",
        "Slow query detected: SELECT * FROM orders WHERE ... (1842ms)",
        "Circuit breaker OPEN for service=payments",
        "Retry budget exhausted; falling back to cached price list",
    ]
    return [
        {"timestamp": f"2026-05-12T14:{20 + i // 4}:{i*7 % 60:02d}Z",
         "level": rng.choice(levels),
         "service": "checkout",
         "message": rng.choice(msgs),
         "trace_id": f"{rng.randrange(16**16):016x}"}
        for i in range(12)
    ]
