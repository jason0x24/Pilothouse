"""End-to-end tests of the runtime in mock-LLM mode."""

from __future__ import annotations

from sqlalchemy import select

from pilothouse.db import session
from pilothouse.models import Agent, Event, EventKind, Run, RunStatus
from pilothouse.orchestration.executor import execute_agent


async def _create_agent(name: str, template: str, params: dict | None = None) -> str:
    async with session() as s:
        a = Agent(name=name, template=template, params=params or {})
        s.add(a)
        await s.flush()
        return a.id


async def test_datadog_triage_mock_run_completes() -> None:
    aid = await _create_agent(
        "triage-test",
        "datadog_alert_triage",
        {"service": "checkout", "slack_channel": "#oncall"},
    )
    run_id = await execute_agent(
        agent_id=aid,
        trigger="manual",
        trigger_payload={"alert_id": "abc-123", "service": "checkout"},
    )
    async with session() as s:
        run = (await s.execute(select(Run).where(Run.id == run_id))).scalar_one()
        events = (
            await s.execute(select(Event).where(Event.run_id == run_id).order_by(Event.created_at))
        ).scalars().all()

    assert run.status == RunStatus.succeeded, run.summary
    assert "## Summary" in run.summary
    # We should have executed at least 4 tool calls (alert, metric, deploys, logs).
    tool_calls = [e for e in events if e.kind == EventKind.tool_call]
    assert len(tool_calls) >= 4
    # Slack post should be in dry-run preview, not real send.
    slack_results = [
        e for e in events if e.kind == EventKind.tool_result and "slack_post_message" in e.data.get("tool", "")
    ]
    if slack_results:
        assert "dry_run" in slack_results[0].data["preview"]


async def test_pr_scanner_with_auto_comment_is_dry_run_safe() -> None:
    aid = await _create_agent(
        "scanner-test",
        "pr_security_scanner",
        {"repo": "acme/api", "auto_comment": True},
    )
    run_id = await execute_agent(
        agent_id=aid,
        trigger="manual",
        trigger_payload={"pull_request": {"number": 99}, "repository": {"full_name": "acme/api"}},
    )
    async with session() as s:
        run = (await s.execute(select(Run).where(Run.id == run_id))).scalar_one()
        events = (
            await s.execute(select(Event).where(Event.run_id == run_id).order_by(Event.created_at))
        ).scalars().all()

    assert run.status == RunStatus.succeeded
    # github_post_pr_comment should appear as a tool_call but be short-circuited
    posts = [e for e in events if e.kind == EventKind.tool_call and e.data.get("tool") == "github_post_pr_comment"]
    assert len(posts) == 1
    post_result = [
        e for e in events
        if e.kind == EventKind.tool_result and e.data.get("tool") == "github_post_pr_comment"
    ][0]
    assert "dry_run" in post_result.data["preview"]


async def test_k8s_investigator_returns_ranked_causes() -> None:
    aid = await _create_agent("k8s-test", "k8s_pod_investigator", {"service": "checkout"})
    run_id = await execute_agent(
        agent_id=aid,
        trigger="manual",
        trigger_payload={
            "commonLabels": {"pod": "checkout-7d8c-xyz", "namespace": "prod", "service": "checkout"}
        },
    )
    async with session() as s:
        run = (await s.execute(select(Run).where(Run.id == run_id))).scalar_one()
    assert run.status == RunStatus.succeeded
    assert "Top 5 candidate causes" in run.summary


async def test_audit_log_is_append_only_and_ordered() -> None:
    aid = await _create_agent("triage-audit", "datadog_alert_triage", {"service": "checkout"})
    run_id = await execute_agent(
        agent_id=aid, trigger="manual", trigger_payload={"alert_id": "x"}
    )
    async with session() as s:
        events = (
            await s.execute(select(Event).where(Event.run_id == run_id).order_by(Event.created_at))
        ).scalars().all()
    assert events[0].kind == EventKind.run_started
    assert events[-1].kind == EventKind.run_finished
    # Tool call should always be followed by a tool_result.
    pending_calls: list[str] = []
    for e in events:
        if e.kind == EventKind.tool_call:
            pending_calls.append(e.data.get("tool_use_id", ""))
        elif e.kind == EventKind.tool_result and pending_calls:
            pending_calls.pop(0)
    assert pending_calls == []
