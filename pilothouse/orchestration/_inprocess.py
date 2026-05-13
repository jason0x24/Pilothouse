"""In-process executor — the original asyncio-based implementation.

Runs everything in the same process that received the trigger. No
external infrastructure required. This is the default executor and
the one your tests / CI exercise unless `PILOTHOUSE_TEMPORAL_ADDRESS`
is set.

Pulled out of `executor.py` so the public surface there can dispatch
to either this implementation or the Temporal one.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from ..agent.runtime import AgentRunner, RunOutcome
from ..config import get_settings
from ..connectors.base import registry as conn_registry
from ..db import session
from ..events import RunEvent, get_bus
from ..models import Agent, Approval, ApprovalStatus, Event, EventKind, Run, RunStatus
from ..templates import registry as tpl_registry
from ._common import fire_hooks_after, fire_hooks_before, persist_outcome

log = logging.getLogger(__name__)


class InProcessExecutor:
    """Executes the agent loop directly in the calling process.

    Stateless — all coordination is via the DB. Safe to construct
    fresh on every call.
    """

    async def execute_agent(
        self,
        *,
        agent_id: str,
        trigger: str,
        trigger_payload: dict,
        dry_run_override: bool | None = None,
    ) -> str:
        settings = get_settings()
        async with session() as s:
            agent = (
                await s.execute(select(Agent).where(Agent.id == agent_id))
            ).scalar_one_or_none()
            if agent is None:
                raise KeyError(f"unknown agent: {agent_id}")
            if not agent.enabled:
                raise RuntimeError(f"agent disabled: {agent.name}")

            template = tpl_registry.get(agent.template)
            params = dict(agent.params or {})
            plan = template.plan(trigger_payload=trigger_payload, params=params)
            tools = conn_registry.tools_for(plan.tool_names)
            dry_run = agent.dry_run if dry_run_override is None else dry_run_override

            # Defensive: pre-multi-tenant agents have no tenant_id; route
            # to default. Bootstrap normally backfills, but the executor
            # must work even before that runs (test races).
            if agent.tenant_id is None:
                from ..tenants import ensure_default_tenant

                agent.tenant_id = await ensure_default_tenant()
                await s.flush()

            run = Run(
                agent_id=agent.id,
                tenant_id=agent.tenant_id,
                trigger=trigger,
                trigger_payload=trigger_payload,
            )
            s.add(run)
            await s.flush()

            # In mock mode the runtime parses a `mock_plan` array from
            # the user message; we wrap the real instruction alongside.
            user_message = plan.user_message
            from ..agent.providers import is_mock_mode

            if is_mock_mode(settings):
                mock_plan = template.mock_plan(
                    trigger_payload=trigger_payload, params=params
                )
                user_message = json.dumps(
                    {"instruction": plan.user_message, "mock_plan": mock_plan}
                )

            run.status = RunStatus.running
            await s.flush()
            rid = run.id
            aid = agent.id
            tid = run.tenant_id or ""

        await fire_hooks_before(rid, aid, tid)
        async with session() as s:
            run = (await s.execute(select(Run).where(Run.id == rid))).scalar_one()
            runner = AgentRunner(s)
            outcome = await runner.start(
                run=run,
                system_prompt=plan.system_prompt,
                user_message=user_message,
                tools=tools,
                dry_run=dry_run,
                require_approval_for_writes=settings.require_approval_for_writes,
                params=params,
                agent_id=aid,
            )
            await persist_outcome(run, outcome)
        await fire_hooks_after(rid, aid, tid, outcome)
        return rid

    async def resume_run(self, run_id: str) -> RunOutcome:
        async with session() as s:
            run = (
                await s.execute(select(Run).where(Run.id == run_id))
            ).scalar_one_or_none()
            if run is None:
                raise KeyError(f"unknown run: {run_id}")
            if run.status != RunStatus.awaiting_approval:
                raise RuntimeError(
                    f"run is not awaiting_approval (status={run.status})"
                )
            agent = (
                await s.execute(select(Agent).where(Agent.id == run.agent_id))
            ).scalar_one()
            template = tpl_registry.get(agent.template)
            params = dict(agent.params or {})
            plan = template.plan(
                trigger_payload=run.trigger_payload or {}, params=params
            )
            tools = conn_registry.tools_for(plan.tool_names)

            run.status = RunStatus.running
            await s.flush()
            runner = AgentRunner(s)
            outcome = await runner.resume(run=run, tools=tools)
            await persist_outcome(run, outcome)
            return outcome

    async def retry_run(
        self, run_id: str, *, dry_run_override: bool | None = None
    ) -> str:
        async with session() as s:
            old = (
                await s.execute(select(Run).where(Run.id == run_id))
            ).scalar_one_or_none()
            if old is None:
                raise KeyError(f"unknown run: {run_id}")
            agent_id = old.agent_id
            payload = dict(old.trigger_payload or {})
            original_trigger = old.trigger

        return await self.execute_agent(
            agent_id=agent_id,
            trigger=f"retry:{original_trigger}",
            trigger_payload=payload,
            dry_run_override=dry_run_override,
        )

    async def cancel_run(self, run_id: str, *, by: str = "operator") -> Run:
        """In-process cancel is a DB flag flip — the runtime polls
        `Run.status` between iterations and exits cleanly. If the run
        is paused at approval we also reject every pending approval so
        sweepers / notifiers see a coherent end state."""
        async with session() as s:
            run = (
                await s.execute(select(Run).where(Run.id == run_id))
            ).scalar_one_or_none()
            if run is None:
                raise KeyError(f"unknown run: {run_id}")
            if run.status in (
                RunStatus.succeeded,
                RunStatus.failed,
                RunStatus.cancelled,
            ):
                return run  # already terminal — idempotent

            if run.status == RunStatus.awaiting_approval:
                pending = (
                    await s.execute(
                        select(Approval).where(
                            Approval.run_id == run_id,
                            Approval.status == ApprovalStatus.pending,
                        )
                    )
                ).scalars().all()
                now = datetime.now(timezone.utc)
                for a in pending:
                    a.status = ApprovalStatus.rejected
                    a.resolved_by = by
                    a.rejection_reason = (
                        "Run cancelled by operator before approval."
                    )
                    a.resolved_at = now
                run.status = RunStatus.cancelled
                run.finished_at = now
                run.summary = run.summary or "Cancelled while awaiting approval."
                ev = Event(
                    run_id=run.id,
                    kind=EventKind.run_cancelled,
                    data={
                        "by": by,
                        "phase": "awaiting_approval",
                        "pending_count": len(pending),
                    },
                )
                s.add(ev)
                get_bus().publish(
                    RunEvent(
                        run_id=run.id,
                        kind=EventKind.run_cancelled.value,
                        data={"by": by},
                    )
                )
                get_bus().record_status("cancelled")
                return run

            # status == running → flip the flag; the runtime exits at
            # the next loop iteration boundary.
            run.status = RunStatus.cancelled
            ev = Event(
                run_id=run.id,
                kind=EventKind.run_cancelled,
                data={
                    "by": by,
                    "phase": run.status.value
                    if hasattr(run.status, "value")
                    else str(run.status),
                },
            )
            s.add(ev)
            get_bus().publish(
                RunEvent(
                    run_id=run.id, kind=EventKind.run_cancelled.value, data={"by": by}
                )
            )
            return run
