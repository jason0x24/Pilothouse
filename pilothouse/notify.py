"""Approval notifications.

When `approval_requested` fires we ping the operator(s) so a paused run
doesn't sit unnoticed. Two destinations are supported:

  * Slack: post a structured message to a channel via the existing bot
    token. Configured via the `notify_slack_channel` field on the agent
    (params) — that scopes notification routing per-agent (e.g. SRE
    approvals go to #sre, security PR scanner approvals go to #appsec).

  * Generic outbound webhook: configured via
    `PILOTHOUSE_NOTIFY_WEBHOOK_URL`. Useful for piping into Opsgenie,
    PagerDuty, Discord, or your own router. POST body is a small JSON
    envelope, NOT the raw event.

The notifier is best-effort: failures are logged but do not block the
runtime. Notifications are fire-and-forget tasks created from the
foreground; they share the event-loop with the runtime but never await
its progress.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import httpx

from .config import get_settings
from .events import RunEvent, get_bus
from .models import Agent, Approval

log = logging.getLogger(__name__)


_listener_task: asyncio.Task | None = None
_listener_queue: asyncio.Queue | None = None


def start_notifier() -> None:
    """Subscribe to the global bus and forward approval_requested events.

    Idempotent. Subscribes to the bus *synchronously* so events emitted
    right after `start_notifier()` returns are still delivered — there
    is no race window. The actual consumption runs as an asyncio task.
    """
    global _listener_task, _listener_queue
    if _listener_task is not None and not _listener_task.done():
        return
    bus = get_bus()
    _listener_queue = bus.subscribe_queue("*")
    _listener_task = asyncio.create_task(
        _run_listener(_listener_queue), name="pilothouse-notifier"
    )


def stop_notifier() -> None:
    global _listener_task, _listener_queue
    if _listener_task is not None:
        _listener_task.cancel()
        _listener_task = None
    if _listener_queue is not None:
        get_bus().unsubscribe("*", _listener_queue)
        _listener_queue = None


async def _run_listener(q: asyncio.Queue) -> None:
    """Routes interesting events to the dispatch path.

      * `approval_requested` → notify operators a human decision is needed
      * `run_terminal` with status in {failed, cancelled} → notify on
        failure (the orchestration layer fires run_terminal exactly once
        per run when a terminal state is reached, so we don't duplicate
        notifications across the per-phase error/cancelled events)
    """
    try:
        while True:
            ev = await q.get()
            if ev.kind == "approval_requested":
                asyncio.create_task(_dispatch_approval(ev))
            elif ev.kind == "run_terminal":
                status = ev.data.get("status")
                if status in ("failed", "cancelled"):
                    asyncio.create_task(_dispatch_run_failure(ev))
    except asyncio.CancelledError:
        return


async def _dispatch_approval(ev: RunEvent) -> None:
    try:
        await _notify_for_event(ev)
    except Exception:  # pragma: no cover — logged for ops
        log.exception("notification dispatch failed for run %s", ev.run_id)


async def _dispatch_run_failure(ev: RunEvent) -> None:
    """Notify on a terminal run state when the agent opts in.

    Opt-in is via `notify_on_failure` (Slack channel) on the agent
    params + the global `PILOTHOUSE_NOTIFY_WEBHOOK_URL`. Event payload
    already carries status / summary / agent_id, so we only need one
    Agent lookup (Agent rows commit before run starts, so they're
    visible from a fresh session — unlike the Run row that triggered
    this event).
    """
    import os
    from sqlalchemy import select

    from .db import session

    try:
        status = ev.data.get("status", "?")
        if status not in {"failed", "cancelled"}:
            return

        agent_id = ev.data.get("agent_id", "")
        agent = None
        if agent_id:
            async with session() as s:
                agent = (
                    await s.execute(select(Agent).where(Agent.id == agent_id))
                ).scalar_one_or_none()

        params = dict(agent.params or {}) if agent else {}
        payload = {
            "kind": "run_failure",
            "agent_name": agent.name if agent else "<unknown>",
            "run_id": ev.run_id,
            "status": status,
            "summary_preview": ev.data.get("summary_preview", ""),
            "tokens_input": ev.data.get("tokens_input", 0),
            "tokens_output": ev.data.get("tokens_output", 0),
        }

        channel = params.get("notify_on_failure")
        if channel:
            await _post_slack_failure(channel, payload)
        url = os.getenv("PILOTHOUSE_NOTIFY_WEBHOOK_URL", "").strip()
        if url:
            await _post_generic(url, payload)
    except Exception:  # pragma: no cover — logged for ops
        log.exception("failure notification failed for run %s", ev.run_id)


async def _post_slack_failure(channel: str, payload: dict) -> None:
    token = get_settings().slack_bot_token
    if not token:
        return
    text = (
        f":x: *Run {payload['status']}* — agent `{payload['agent_name']}`.\n"
        f"Run: `{payload['run_id'][:8]}`\n"
        f"```{payload['summary_preview']}```"
    )
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {token}"},
                json={"channel": channel, "text": text},
            )
    except Exception:
        log.exception("slack failure notify failed")


async def _notify_for_event(ev: RunEvent) -> None:
    """Build the notification payload from the event data alone.

    We deliberately don't query the DB here because the runtime emits
    `approval_requested` from inside an uncommitted transaction. By the
    time the listener task runs we'd see a stale snapshot. All necessary
    data is therefore stuffed into the event payload at emit time.

    For agent name we fall back to a DB lookup because it's stored on
    Agent (not Approval), and re-emitting it on every event would bloat
    the payload. If the lookup misses (mid-transaction), we degrade to
    "<unknown>" rather than swallow the notification.
    """
    from sqlalchemy import select

    from .db import session

    data = ev.data
    approval_id = data.get("approval_id", "")
    agent_id = data.get("agent_id", "")
    agent_name = "<unknown>"
    if agent_id:
        try:
            async with session() as s:
                a = (
                    await s.execute(select(Agent).where(Agent.id == agent_id))
                ).scalar_one_or_none()
                if a is not None:
                    agent_name = a.name
        except Exception:
            pass

    params = dict(data.get("params") or {})
    payload = {
        "kind": "approval_requested",
        "agent_name": agent_name,
        "run_id": ev.run_id,
        "approval_id": approval_id,
        "tool": data.get("tool", ""),
        "rationale": data.get("rationale", ""),
        "tool_input": data.get("input", {}),
    }

    # Slack-channel routing per agent.
    channel = params.get("notify_slack_channel")
    if channel:
        await _post_slack(channel, payload)

    # Generic outbound webhook.
    url = os.getenv("PILOTHOUSE_NOTIFY_WEBHOOK_URL", "").strip()
    if url:
        await _post_generic(url, payload)


async def _post_slack(channel: str, payload: dict) -> None:
    """Post an approval-request message to Slack with interactive buttons.

    The buttons carry the approval_id in `value`. When clicked, Slack
    POSTs to our `/webhooks/slack/interactivity` endpoint, which
    resolves the approval and posts a follow-up confirming the action.
    """
    token = get_settings().slack_bot_token
    if not token:
        log.info("notify_slack_channel set but PILOTHOUSE_SLACK_BOT_TOKEN missing")
        return
    text = (
        f":pause_button: *Approval needed* — agent `{payload['agent_name']}` "
        f"wants to run `{payload['tool']}`."
    )
    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": text},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Run*\n`{payload['run_id'][:8]}`"},
                {"type": "mrkdwn", "text": f"*Approval*\n`{payload['approval_id'][:8]}`"},
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"```{json.dumps(payload['tool_input'], indent=2)[:1500]}```",
            },
        },
        {
            "type": "actions",
            "block_id": "pilothouse_approval",
            "elements": [
                {
                    "type": "button",
                    "action_id": "approve",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "value": payload["approval_id"],
                    "confirm": {
                        "title": {"type": "plain_text", "text": "Approve action?"},
                        "text": {
                            "type": "mrkdwn",
                            "text": f"Run `{payload['tool']}` for real (not dry-run).",
                        },
                        "confirm": {"type": "plain_text", "text": "Approve"},
                        "deny": {"type": "plain_text", "text": "Cancel"},
                    },
                },
                {
                    "type": "button",
                    "action_id": "reject",
                    "style": "danger",
                    "text": {"type": "plain_text", "text": "Reject"},
                    "value": payload["approval_id"],
                },
            ],
        },
    ]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {token}"},
                json={"channel": channel, "text": text, "blocks": blocks},
            )
            if not r.is_success:
                log.warning("slack notify HTTP %s: %s", r.status_code, r.text[:200])
    except Exception:
        log.exception("slack notify failed")


async def _post_generic(url: str, payload: dict) -> None:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json=payload)
            if not r.is_success:
                log.warning("generic notify HTTP %s: %s", r.status_code, r.text[:200])
    except Exception:
        log.exception("generic notify failed")
