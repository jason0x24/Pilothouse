"""Run retry tests."""

from __future__ import annotations

from sqlalchemy import select

from pilothouse.db import session
from pilothouse.models import Agent, Run
from pilothouse.orchestration.executor import execute_agent, retry_run


async def _create_agent() -> str:
    async with session() as s:
        a = Agent(name="retry-test", template="datadog_alert_triage", params={"service": "checkout"})
        s.add(a)
        await s.flush()
        return a.id


async def test_retry_creates_a_new_run_with_same_payload() -> None:
    aid = await _create_agent()
    original = await execute_agent(
        agent_id=aid,
        trigger="manual",
        trigger_payload={"alert_id": "abc", "extra": "carry"},
    )
    new_id = await retry_run(original)
    assert new_id != original
    async with session() as s:
        old = (await s.execute(select(Run).where(Run.id == original))).scalar_one()
        new = (await s.execute(select(Run).where(Run.id == new_id))).scalar_one()
    assert new.agent_id == old.agent_id
    assert new.trigger_payload == old.trigger_payload
    assert new.trigger.startswith("retry:")


async def test_retry_unknown_run_raises() -> None:
    import pytest

    with pytest.raises(KeyError):
        await retry_run("nope")
