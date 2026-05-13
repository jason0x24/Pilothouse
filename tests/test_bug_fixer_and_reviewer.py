"""End-to-end mock-mode tests for the new templates."""

from __future__ import annotations

from sqlalchemy import select

from pilothouse.db import session
from pilothouse.models import Agent, Approval, Event, EventKind, Run, RunStatus
from pilothouse.orchestration.executor import execute_agent


async def _create(name: str, template: str, params: dict, *, dry_run: bool = True) -> str:
    async with session() as s:
        a = Agent(name=name, template=template, params=params, dry_run=dry_run)
        s.add(a)
        await s.flush()
        return a.id


# --- bug_auto_fixer -----------------------------------------------------


async def test_bug_auto_fixer_walks_full_pipeline_in_dry_run() -> None:
    aid = await _create(
        "bug-fix",
        "bug_auto_fixer",
        {"repo": "acme/api", "ticket_id_override": "ENG-1234"},
    )
    run_id = await execute_agent(
        agent_id=aid,
        trigger="manual",
        trigger_payload={"issue": {"identifier": "ENG-1234"}},
    )
    async with session() as s:
        run = (await s.execute(select(Run).where(Run.id == run_id))).scalar_one()
        events = (
            await s.execute(select(Event).where(Event.run_id == run_id))
        ).scalars().all()
    assert run.status == RunStatus.succeeded, run.summary
    called = [e.data["tool"] for e in events if e.kind == EventKind.tool_call]
    # Must hit: read ticket → read file → branch → commit → PR → comment back.
    assert "linear_get_issue" in called
    assert "github_get_file_content" in called
    assert "github_create_branch" in called
    assert "github_create_or_update_file" in called
    assert "github_create_pull_request" in called
    assert "linear_add_comment" in called
    # All destructive ops should have a dry_run preview as the result.
    destructive_results = [
        e for e in events
        if e.kind == EventKind.tool_result
        and e.data.get("tool") in (
            "github_create_branch",
            "github_create_or_update_file",
            "github_create_pull_request",
            "linear_add_comment",
        )
    ]
    for ev in destructive_results:
        assert "dry_run" in ev.data["preview"]
    # Branch in commit input follows the convention.
    branch_calls = [
        e.data for e in events
        if e.kind == EventKind.tool_call and e.data.get("tool") == "github_create_branch"
    ]
    assert branch_calls
    branch_name = branch_calls[0]["input"]["branch"]
    assert branch_name.startswith("pilothouse/fix/ENG-1234-")


async def test_bug_auto_fixer_cron_mode_lists_first() -> None:
    """No ticket id in payload → cron path → must call linear_list_issues first."""
    aid = await _create(
        "bug-fix-cron", "bug_auto_fixer", {"repo": "acme/api", "team_key": "ENG"}
    )
    run_id = await execute_agent(agent_id=aid, trigger="cron", trigger_payload={})
    async with session() as s:
        events = (
            await s.execute(select(Event).where(Event.run_id == run_id))
        ).scalars().all()
    called = [e.data["tool"] for e in events if e.kind == EventKind.tool_call]
    assert called[0] == "linear_list_issues"


async def test_bug_auto_fixer_destructive_pauses_for_approval_when_live() -> None:
    """With dry_run=false, the first destructive tool (branch creation)
    pauses the run for approval — same gate as every other write."""
    aid = await _create(
        "bug-fix-live",
        "bug_auto_fixer",
        {"repo": "acme/api", "ticket_id_override": "ENG-1234"},
        dry_run=False,
    )
    run_id = await execute_agent(
        agent_id=aid,
        trigger="manual",
        trigger_payload={"issue": {"identifier": "ENG-1234"}},
    )
    async with session() as s:
        run = (await s.execute(select(Run).where(Run.id == run_id))).scalar_one()
        approvals = (
            await s.execute(select(Approval).where(Approval.run_id == run_id))
        ).scalars().all()
    assert run.status == RunStatus.awaiting_approval
    assert any(a.tool_name == "github_create_branch" for a in approvals)


# --- pr_code_reviewer ---------------------------------------------------


async def test_pr_code_reviewer_posts_one_review_with_inline_comments() -> None:
    aid = await _create(
        "reviewer",
        "pr_code_reviewer",
        {"repo": "acme/api", "dimensions": ["correctness", "tests"]},
    )
    run_id = await execute_agent(
        agent_id=aid,
        trigger="manual",
        trigger_payload={
            "pull_request": {"number": 99},
            "repository": {"full_name": "acme/api"},
        },
    )
    async with session() as s:
        run = (await s.execute(select(Run).where(Run.id == run_id))).scalar_one()
        events = (
            await s.execute(select(Event).where(Event.run_id == run_id))
        ).scalars().all()
    assert run.status == RunStatus.succeeded
    review_calls = [
        e.data for e in events
        if e.kind == EventKind.tool_call and e.data["tool"] == "github_create_pr_review"
    ]
    assert len(review_calls) == 1
    review_input = review_calls[0]["input"]
    assert review_input["event"] in {"APPROVE", "REQUEST_CHANGES", "COMMENT"}
    assert review_input["comments"], "expected at least one inline comment"
    # Inline comment must carry path + line + body.
    for c in review_input["comments"]:
        assert {"path", "line", "body"}.issubset(c.keys())


async def test_all_eight_templates_registered() -> None:
    from pilothouse.templates.base import registry

    expected = {
        "datadog_alert_triage",
        "pr_security_scanner",
        "k8s_pod_investigator",
        "terraform_plan_reviewer",
        "pagerduty_first_responder",
        "flaky_test_hunter",
        "bug_auto_fixer",
        "pr_code_reviewer",
    }
    assert expected.issubset(set(registry.templates.keys()))
