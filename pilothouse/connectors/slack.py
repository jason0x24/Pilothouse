"""Slack connector — minimal: post message + lookup channel."""

from __future__ import annotations

import httpx

from ..config import get_settings
from .base import Connector, ToolContext, ToolResult


class SlackConnector(Connector):
    name = "slack"

    def __init__(self) -> None:
        super().__init__()
        self._add(
            "slack_post_message",
            "Post a message to a Slack channel. DESTRUCTIVE: writes to Slack.",
            {
                "type": "object",
                "properties": {
                    "channel": {
                        "type": "string",
                        "description": "Channel ID or name with leading #",
                    },
                    "text": {"type": "string"},
                    "blocks": {"type": "array", "items": {"type": "object"}, "default": []},
                },
                "required": ["channel", "text"],
            },
            self._post_message,
            is_destructive=True,
        )

    @property
    def live(self) -> bool:
        return bool(get_settings().slack_bot_token)

    async def _post_message(self, ctx: ToolContext, params: dict) -> ToolResult:
        if ctx.dry_run:
            return ToolResult(
                content={
                    "dry_run": True,
                    "would_post": {
                        "channel": params["channel"],
                        "text_preview": params["text"][:280],
                        "block_count": len(params.get("blocks") or []),
                    },
                }
            )
        if not self.live:
            return ToolResult(content={"error": "slack_bot_token not configured"}, is_error=True)
        s = get_settings()
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {s.slack_bot_token}"},
                json={
                    "channel": params["channel"],
                    "text": params["text"],
                    "blocks": params.get("blocks") or [],
                },
            )
        return ToolResult(content=r.json(), is_error=r.is_error)
