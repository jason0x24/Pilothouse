"""PagerDuty connector — incident read + ack/resolve writes."""

from __future__ import annotations

import hashlib
import random
from typing import Any

import httpx

from ..config import get_settings
from .base import Connector, ToolContext, ToolResult


class PagerDutyConnector(Connector):
    name = "pagerduty"

    def __init__(self) -> None:
        super().__init__()
        self._add(
            "pagerduty_get_incident",
            "Fetch a PagerDuty incident by ID.",
            {
                "type": "object",
                "properties": {"incident_id": {"type": "string"}},
                "required": ["incident_id"],
            },
            self._get_incident,
        )
        self._add(
            "pagerduty_add_note",
            "Add an investigation note to an incident. DESTRUCTIVE.",
            {
                "type": "object",
                "properties": {
                    "incident_id": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["incident_id", "content"],
            },
            self._add_note,
            is_destructive=True,
        )
        self._add(
            "pagerduty_acknowledge",
            "Acknowledge an incident. DESTRUCTIVE.",
            {
                "type": "object",
                "properties": {"incident_id": {"type": "string"}},
                "required": ["incident_id"],
            },
            self._ack,
            is_destructive=True,
        )

    @property
    def live(self) -> bool:
        return bool(get_settings().pagerduty_token)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Token token={get_settings().pagerduty_token}",
            "Accept": "application/vnd.pagerduty+json;version=2",
            "Content-Type": "application/json",
        }

    async def _get_incident(self, ctx: ToolContext, params: dict) -> ToolResult:
        if not self.live:
            return ToolResult(content=_mock_incident(params["incident_id"]))
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"https://api.pagerduty.com/incidents/{params['incident_id']}",
                headers=self._headers(),
            )
        return ToolResult(content=r.json(), is_error=r.is_error)

    async def _add_note(self, ctx: ToolContext, params: dict) -> ToolResult:
        if ctx.dry_run:
            return ToolResult(content={"dry_run": True, "would_add_note": params})
        if not self.live:
            return ToolResult(content={"error": "no token"}, is_error=True)
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"https://api.pagerduty.com/incidents/{params['incident_id']}/notes",
                headers=self._headers(),
                json={"note": {"content": params["content"]}},
            )
        return ToolResult(content=r.json(), is_error=r.is_error)

    async def _ack(self, ctx: ToolContext, params: dict) -> ToolResult:
        if ctx.dry_run:
            return ToolResult(content={"dry_run": True, "would_ack": params["incident_id"]})
        if not self.live:
            return ToolResult(content={"error": "no token"}, is_error=True)
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.put(
                f"https://api.pagerduty.com/incidents/{params['incident_id']}",
                headers=self._headers(),
                json={"incident": {"type": "incident_reference", "status": "acknowledged"}},
            )
        return ToolResult(content=r.json(), is_error=r.is_error)


def _seed(*parts: Any) -> random.Random:
    h = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()
    return random.Random(int(h[:8], 16))


def _mock_incident(incident_id: str) -> dict:
    rng = _seed("pd", incident_id)
    return {
        "id": incident_id,
        "incident_number": rng.randint(1000, 9999),
        "title": "checkout p95 latency SLO breach",
        "urgency": rng.choice(["high", "high", "low"]),
        "status": "triggered",
        "service": {"summary": "checkout"},
        "created_at": "2026-05-12T14:21:00Z",
        "html_url": f"https://example.pagerduty.com/incidents/{incident_id}",
    }
