"""Unit tests for connector mock-mode behaviour and dry-run gating."""

from __future__ import annotations

import asyncio

from pilothouse.connectors.base import ToolContext, registry


async def _emit(_name: str, _data: dict) -> None:
    return None


def _ctx(dry_run: bool = True) -> ToolContext:
    return ToolContext(
        run_id="r", agent_id="a", dry_run=dry_run, params={}, emit=_emit
    )


def test_registry_has_all_builtin_connectors() -> None:
    names = set(registry.connectors.keys())
    assert {"datadog", "github", "pagerduty", "slack"}.issubset(names)


async def test_datadog_get_alert_mock_is_deterministic() -> None:
    tool = registry.all_tools()["datadog_get_alert"]
    r1 = await tool.handler(_ctx(), {"alert_id": "12345"})
    r2 = await tool.handler(_ctx(), {"alert_id": "12345"})
    assert r1.content == r2.content
    assert r1.content["id"] == "12345"


async def test_github_post_comment_short_circuits_in_dry_run() -> None:
    tool = registry.all_tools()["github_post_pr_comment"]
    res = await tool.handler(
        _ctx(dry_run=True), {"repo": "x/y", "pr_number": 1, "body": "hi"}
    )
    assert res.content["dry_run"] is True
    assert res.is_error is False


async def test_slack_post_message_short_circuits_in_dry_run() -> None:
    tool = registry.all_tools()["slack_post_message"]
    res = await tool.handler(_ctx(dry_run=True), {"channel": "#x", "text": "y"})
    assert res.content["dry_run"] is True


def test_destructive_flag_is_set_correctly() -> None:
    tools = registry.all_tools()
    assert tools["github_post_pr_comment"].is_destructive
    assert tools["slack_post_message"].is_destructive
    assert tools["pagerduty_acknowledge"].is_destructive
    assert not tools["datadog_get_alert"].is_destructive
    assert not tools["github_get_pr"].is_destructive


async def test_diff_mock_sometimes_contains_secret() -> None:
    """Statistical check: across many seeds at least one diff has the secret pattern."""
    tool = registry.all_tools()["github_get_pr_diff"]
    seen = False
    for n in range(1, 30):
        res = await tool.handler(_ctx(), {"repo": "acme/api", "pr_number": n})
        if "AKIA" in res.content:
            seen = True
            break
    assert seen, "expected at least one mock diff to include the secret pattern"


def _sync(coro):
    return asyncio.get_event_loop().run_until_complete(coro)
