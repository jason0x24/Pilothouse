"""Kubernetes connector tests (mock mode)."""

from __future__ import annotations

from pilothouse.connectors.base import ToolContext, registry


async def _emit(_n: str, _d: dict) -> None:
    return None


def _ctx() -> ToolContext:
    return ToolContext(run_id="r", agent_id="a", dry_run=True, params={}, emit=_emit)


def test_kubernetes_tools_registered() -> None:
    tools = registry.all_tools()
    for name in (
        "kubernetes_describe_pod",
        "kubernetes_get_pod_events",
        "kubernetes_get_pod_logs",
        "kubernetes_list_pods",
    ):
        assert name in tools, f"missing tool {name}"
        # All kubernetes tools are read-only.
        assert not tools[name].is_destructive


async def test_describe_pod_mock_is_deterministic() -> None:
    tool = registry.all_tools()["kubernetes_describe_pod"]
    r1 = await tool.handler(_ctx(), {"namespace": "prod", "name": "checkout-x"})
    r2 = await tool.handler(_ctx(), {"namespace": "prod", "name": "checkout-x"})
    assert r1.content == r2.content
    assert r1.content["metadata"]["name"] == "checkout-x"
    assert r1.content["metadata"]["namespace"] == "prod"
    cs = r1.content["status"]["containerStatuses"][0]
    assert cs["restartCount"] > 0


async def test_events_mock_returns_eventlist_shape() -> None:
    tool = registry.all_tools()["kubernetes_get_pod_events"]
    r = await tool.handler(_ctx(), {"namespace": "prod", "name": "p", "limit": 5})
    assert r.content["kind"] == "EventList"
    assert isinstance(r.content["items"], list) and r.content["items"]


async def test_pod_logs_mock_returns_text() -> None:
    tool = registry.all_tools()["kubernetes_get_pod_logs"]
    r = await tool.handler(
        _ctx(), {"namespace": "prod", "name": "p", "tail_lines": 10, "previous": True}
    )
    assert isinstance(r.content, str)
    assert "\n" in r.content


async def test_k8s_template_executes_kubernetes_tools() -> None:
    from sqlalchemy import select

    from pilothouse.db import session
    from pilothouse.models import Agent, Event, EventKind, Run, RunStatus
    from pilothouse.orchestration.executor import execute_agent

    async with session() as s:
        a = Agent(name="k8s-conn-test", template="k8s_pod_investigator", params={})
        s.add(a)
        await s.flush()
        aid = a.id
    run_id = await execute_agent(
        agent_id=aid,
        trigger="manual",
        trigger_payload={
            "commonLabels": {"pod": "checkout-7d8c-xyz", "namespace": "prod", "service": "checkout"}
        },
    )
    async with session() as s:
        run = (await s.execute(select(Run).where(Run.id == run_id))).scalar_one()
        events = (
            await s.execute(select(Event).where(Event.run_id == run_id))
        ).scalars().all()
    assert run.status == RunStatus.succeeded
    called = {e.data["tool"] for e in events if e.kind == EventKind.tool_call}
    # The mock plan exercises three kubernetes tools end-to-end.
    assert "kubernetes_describe_pod" in called
    assert "kubernetes_get_pod_events" in called
    assert "kubernetes_get_pod_logs" in called
