"""Tests for the approval flow: pause, persist, resume."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from pilothouse.db import session
from pilothouse.models import Agent, Approval, ApprovalStatus, Event, EventKind, Run, RunStatus
from pilothouse.orchestration.executor import execute_agent, resume_run


async def _create_agent(name: str, template: str, params: dict, *, dry_run: bool) -> str:
    async with session() as s:
        a = Agent(name=name, template=template, params=params, dry_run=dry_run)
        s.add(a)
        await s.flush()
        return a.id


async def test_run_pauses_when_destructive_tool_needs_approval() -> None:
    """With dry_run=False and require_approval_for_writes=True (default),
    posting a PR comment must trigger an Approval and pause the run."""
    aid = await _create_agent(
        "scanner-approval",
        "pr_security_scanner",
        {"repo": "acme/api", "auto_comment": True},
        dry_run=False,
    )
    run_id = await execute_agent(
        agent_id=aid,
        trigger="manual",
        trigger_payload={"pull_request": {"number": 42}, "repository": {"full_name": "acme/api"}},
    )
    async with session() as s:
        run = (await s.execute(select(Run).where(Run.id == run_id))).scalar_one()
        approvals = (
            await s.execute(select(Approval).where(Approval.run_id == run_id))
        ).scalars().all()

    assert run.status == RunStatus.awaiting_approval, run.summary
    assert run.state_json, "expected serialized state for resume"
    assert len(approvals) == 1
    ap = approvals[0]
    assert ap.tool_name == "github_post_pr_comment"
    assert ap.status == ApprovalStatus.pending
    # The rationale should carry the assistant's prose; in mock mode this
    # may be empty, but the field must exist either way.
    assert isinstance(ap.rationale, str)


async def test_resume_after_approve_runs_destructive_tool_for_real() -> None:
    aid = await _create_agent(
        "scanner-approve",
        "pr_security_scanner",
        {"repo": "acme/api", "auto_comment": True},
        dry_run=False,
    )
    run_id = await execute_agent(
        agent_id=aid,
        trigger="manual",
        trigger_payload={"pull_request": {"number": 7}, "repository": {"full_name": "acme/api"}},
    )
    async with session() as s:
        ap = (
            await s.execute(select(Approval).where(Approval.run_id == run_id))
        ).scalars().one()
        ap.status = ApprovalStatus.approved
        ap.resolved_by = "tester"
        ap.resolved_at = datetime.now(timezone.utc)

    outcome = await resume_run(run_id)
    assert outcome.status == RunStatus.succeeded, outcome.summary

    async with session() as s:
        events = (
            await s.execute(
                select(Event).where(Event.run_id == run_id).order_by(Event.created_at)
            )
        ).scalars().all()
    # The post-approval tool_result should NOT be the dry-run preview — it
    # should be the live mock result (which, since github_token isn't set,
    # produces an error). What matters for this test is the post_approval flag.
    post_approval = [
        e for e in events
        if e.kind == EventKind.tool_result and e.data.get("post_approval")
    ]
    assert post_approval, "expected at least one post-approval tool_result event"

    resolved = [e for e in events if e.kind == EventKind.approval_resolved]
    assert resolved and resolved[0].data["decision"] == "approved"


async def test_resume_after_reject_skips_tool_with_rejection_payload() -> None:
    aid = await _create_agent(
        "scanner-reject",
        "pr_security_scanner",
        {"repo": "acme/api", "auto_comment": True},
        dry_run=False,
    )
    run_id = await execute_agent(
        agent_id=aid,
        trigger="manual",
        trigger_payload={"pull_request": {"number": 9}, "repository": {"full_name": "acme/api"}},
    )
    async with session() as s:
        ap = (
            await s.execute(select(Approval).where(Approval.run_id == run_id))
        ).scalars().one()
        ap.status = ApprovalStatus.rejected
        ap.resolved_by = "tester"
        ap.rejection_reason = "PR author should rotate the secret first."
        ap.resolved_at = datetime.now(timezone.utc)

    outcome = await resume_run(run_id)
    assert outcome.status == RunStatus.succeeded

    async with session() as s:
        events = (
            await s.execute(
                select(Event).where(Event.run_id == run_id).order_by(Event.created_at)
            )
        ).scalars().all()
    resolved = [e for e in events if e.kind == EventKind.approval_resolved]
    assert resolved and resolved[0].data["decision"] == "rejected"
    assert "rotate" in resolved[0].data["reason"]


async def test_dry_run_bypasses_approval_gate() -> None:
    """When the agent is in dry_run, even a destructive tool short-circuits
    immediately with a preview — no Approval should be created."""
    aid = await _create_agent(
        "scanner-dry",
        "pr_security_scanner",
        {"repo": "acme/api", "auto_comment": True},
        dry_run=True,
    )
    run_id = await execute_agent(
        agent_id=aid,
        trigger="manual",
        trigger_payload={"pull_request": {"number": 11}, "repository": {"full_name": "acme/api"}},
    )
    async with session() as s:
        run = (await s.execute(select(Run).where(Run.id == run_id))).scalar_one()
        approvals = (
            await s.execute(select(Approval).where(Approval.run_id == run_id))
        ).scalars().all()

    assert run.status == RunStatus.succeeded
    assert approvals == []
