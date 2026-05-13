"""Smoke tests for the three templates added in the second push."""

from __future__ import annotations

from sqlalchemy import select

from pilothouse.db import session
from pilothouse.models import Agent, Event, EventKind, Run, RunStatus
from pilothouse.orchestration.executor import execute_agent


async def _create(name: str, template: str, params: dict) -> str:
    async with session() as s:
        a = Agent(name=name, template=template, params=params, dry_run=True)
        s.add(a)
        await s.flush()
        return a.id


async def test_terraform_plan_reviewer_executes_full_mock_plan() -> None:
    aid = await _create(
        "tf-test",
        "terraform_plan_reviewer",
        {"repo": "acme/infra", "auto_comment": True},
    )
    run_id = await execute_agent(
        agent_id=aid,
        trigger="manual",
        trigger_payload={
            "pull_request": {"number": 88},
            "repository": {"full_name": "acme/infra"},
        },
    )
    async with session() as s:
        run = (await s.execute(select(Run).where(Run.id == run_id))).scalar_one()
        events = (
            await s.execute(select(Event).where(Event.run_id == run_id))
        ).scalars().all()
    assert run.status == RunStatus.succeeded
    assert "BLOCKING" in run.summary
    called = {e.data["tool"] for e in events if e.kind == EventKind.tool_call}
    assert {"github_get_pr", "github_get_pr_diff", "github_list_recent_commits"}.issubset(called)
    assert "github_post_pr_comment" in called  # auto_comment branch


async def test_pagerduty_first_responder_does_not_acknowledge() -> None:
    aid = await _create(
        "pd-test",
        "pagerduty_first_responder",
        {"service": "checkout", "slack_channel": "#oncall", "runbook": "https://runbooks/checkout"},
    )
    run_id = await execute_agent(
        agent_id=aid,
        trigger="manual",
        trigger_payload={"incident": {"id": "PINC42"}},
    )
    async with session() as s:
        run = (await s.execute(select(Run).where(Run.id == run_id))).scalar_one()
        events = (
            await s.execute(select(Event).where(Event.run_id == run_id))
        ).scalars().all()
    assert run.status == RunStatus.succeeded
    called = {e.data["tool"] for e in events if e.kind == EventKind.tool_call}
    # Should call PD read + Slack + DD + PD note, but NOT acknowledge.
    assert "pagerduty_get_incident" in called
    assert "slack_post_message" in called
    assert "datadog_query_metric" in called
    assert "pagerduty_add_note" in called
    assert "pagerduty_acknowledge" not in called


async def test_flaky_test_hunter_produces_digest() -> None:
    aid = await _create(
        "flaky-test",
        "flaky_test_hunter",
        {"repo": "acme/api", "tracking_issue": 1, "auto_comment": True},
    )
    run_id = await execute_agent(
        agent_id=aid,
        trigger="cron",
        trigger_payload={},
    )
    async with session() as s:
        run = (await s.execute(select(Run).where(Run.id == run_id))).scalar_one()
    assert run.status == RunStatus.succeeded
    assert "Flaky tests" in run.summary
    assert "test_circuit_breaker" in run.summary


async def test_all_six_templates_registered() -> None:
    """All six built-in templates must be present in the registry.
    Other templates (e.g. test-only ones registered by sibling tests) may
    also be present — we assert subset, not exact equality."""
    from pilothouse.templates.base import registry

    expected = {
        "datadog_alert_triage",
        "pr_security_scanner",
        "k8s_pod_investigator",
        "terraform_plan_reviewer",
        "pagerduty_first_responder",
        "flaky_test_hunter",
    }
    assert expected.issubset(set(registry.templates.keys()))
