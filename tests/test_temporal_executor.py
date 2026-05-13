"""Temporal executor tests — gated on `temporalio` being installed.

These tests fire up an in-process Temporal dev server via
`WorkflowEnvironment.start_local()`, exercise the same set of
operations as the in-process executor, and assert behaviour is
identical. They're skipped automatically when the optional
`temporalio` dependency isn't available, so the default `pytest` run
on a stock install never touches Temporal.
"""

from __future__ import annotations

import pytest

temporalio = pytest.importorskip("temporalio")  # noqa: F841 — skip whole file if absent

from sqlalchemy import select

from pilothouse.db import session
from pilothouse.models import Agent, Run, RunStatus
from pilothouse.orchestration import executor as ex
from pilothouse.orchestration._temporal import TemporalExecutor


@pytest.fixture
async def temporal_executor(monkeypatch):
    """A fresh TemporalExecutor backed by an in-process dev server.

    We patch the dispatcher's cached executor so the public functions
    (`execute_agent`, `cancel_run`, …) all route through this same
    instance — that's what real Temporal-mode deployments do.
    """
    monkeypatch.setenv("PILOTHOUSE_TEMPORAL_ADDRESS", "dev")
    monkeypatch.setenv("PILOTHOUSE_TEMPORAL_NAMESPACE", "default")
    monkeypatch.setenv("PILOTHOUSE_TEMPORAL_TASK_QUEUE", "pilothouse-test")
    from pilothouse.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    ex.reset_executor()

    executor = TemporalExecutor(
        address="dev", namespace="default", task_queue="pilothouse-test"
    )
    # Wire it into the cached slot so module-level `execute_agent` etc.
    # routes through us.
    ex._cached_executor = executor

    try:
        yield executor
    finally:
        await executor.shutdown()
        ex.reset_executor()
        get_settings.cache_clear()  # type: ignore[attr-defined]


async def _create_agent(name: str = "temporal-test") -> str:
    async with session() as s:
        a = Agent(
            name=name,
            template="datadog_alert_triage",
            params={"service": "checkout"},
            dry_run=True,
        )
        s.add(a)
        await s.flush()
        return a.id


# --- happy-path -------------------------------------------------------


async def test_execute_agent_under_temporal_succeeds(temporal_executor) -> None:
    aid = await _create_agent()
    run_id = await ex.execute_agent(
        agent_id=aid, trigger="manual", trigger_payload={"alert_id": "x"}
    )
    async with session() as s:
        run = (await s.execute(select(Run).where(Run.id == run_id))).scalar_one()
    assert run.status == RunStatus.succeeded, run.summary
    # Cost + tokens should be populated by the shared persist_outcome
    # helper, regardless of which executor produced the run.
    assert run.tokens_input > 0


async def test_executor_kind_reports_temporal(temporal_executor) -> None:
    assert ex.executor_kind().startswith("temporal(")


# --- retry routes through Temporal too -------------------------------


async def test_retry_creates_a_second_workflow(temporal_executor) -> None:
    aid = await _create_agent("temporal-retry")
    original = await ex.execute_agent(
        agent_id=aid, trigger="manual", trigger_payload={"i": 1}
    )
    retried = await ex.retry_run(original)
    assert retried != original
    async with session() as s:
        old = (await s.execute(select(Run).where(Run.id == original))).scalar_one()
        new = (await s.execute(select(Run).where(Run.id == retried))).scalar_one()
    assert new.agent_id == old.agent_id
    assert new.trigger.startswith("retry:")


# --- cancellation -----------------------------------------------------


async def test_cancel_terminal_run_is_idempotent(temporal_executor) -> None:
    """Cancel after the workflow has finished should not raise — the
    Temporal cancel signal may miss, but the DB-side cancel logic
    short-circuits on terminal status."""
    aid = await _create_agent("temporal-cancel")
    run_id = await ex.execute_agent(
        agent_id=aid, trigger="manual", trigger_payload={"alert_id": "x"}
    )
    # Run completes via mock plan; cancel after the fact.
    await ex.cancel_run(run_id, by="test")
    async with session() as s:
        run = (await s.execute(select(Run).where(Run.id == run_id))).scalar_one()
    # Terminal status preserved — cancel did not overwrite succeeded.
    assert run.status == RunStatus.succeeded
