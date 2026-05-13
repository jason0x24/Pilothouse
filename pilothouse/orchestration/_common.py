"""Helpers shared by every executor implementation.

Persisting the run outcome, firing lifecycle hooks, and the pricing
constants don't depend on whether the agent loop ran in-process or
inside a Temporal Activity — they take an `(run, outcome)` pair and
write the same fields to the DB / bus. Keeping them here lets the
in-process and Temporal executors share identical persistence semantics
with zero duplication.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from ..agent.runtime import RunOutcome
from ..events import RunEvent, get_bus
from ..models import EventKind, Run, RunStatus

log = logging.getLogger(__name__)

# Pricing snapshot used purely for the per-run cost estimate stored on Run.
# x100 to keep an integer column (cents x 100 → fractional cents).
PRICE_IN_PER_M_USD = 15.0
PRICE_OUT_PER_M_USD = 75.0


async def persist_outcome(run: Run, outcome: RunOutcome) -> None:
    """Write the outcome to the Run row and publish a `run_terminal` event.

    Called from inside an open session — caller commits.
    """
    run.status = outcome.status
    run.summary = outcome.summary
    run.tokens_input = outcome.tokens_input
    run.tokens_output = outcome.tokens_output
    run.cost_usd_cents = int(
        outcome.tokens_input * PRICE_IN_PER_M_USD / 1_000_000 * 100 * 100
        + outcome.tokens_output * PRICE_OUT_PER_M_USD / 1_000_000 * 100 * 100
    )
    status_v = (
        outcome.status.value if hasattr(outcome.status, "value") else str(outcome.status)
    )
    is_terminal = outcome.status in (
        RunStatus.succeeded,
        RunStatus.failed,
        RunStatus.cancelled,
    )
    if is_terminal:
        run.finished_at = datetime.now(timezone.utc)
    get_bus().record_status(status_v)
    if is_terminal:
        # Carry agent_id + tenant_id directly on the event so subscribers
        # don't have to read the Run row in a fresh session — that would
        # race the still-uncommitted parent transaction.
        get_bus().publish(
            RunEvent(
                run_id=run.id,
                kind=EventKind.run_terminal.value,
                data={
                    "status": status_v,
                    "tokens_input": outcome.tokens_input,
                    "tokens_output": outcome.tokens_output,
                    "summary_preview": (outcome.summary or "")[:280],
                    "tenant_id": run.tenant_id,
                    "agent_id": run.agent_id,
                },
            )
        )


async def fire_hooks_before(run_id: str, agent_id: str, tenant_id: str) -> None:
    """Run every enabled HookPlugin's `before_run`. Swallow per-hook
    exceptions so one buggy plugin can't break orchestration."""
    try:
        from ..plugins.manager import get_manager

        for hook in get_manager().get_hooks():
            try:
                await hook.before_run(
                    run_id=run_id, agent_id=agent_id, tenant_id=tenant_id
                )
            except Exception:
                log.exception("hook %s before_run raised", hook.name)
    except Exception:
        # Plugin manager may not be initialised in CLI flows that bypass
        # the API lifespan — that's fine, hooks are optional.
        pass


async def fire_hooks_after(
    run_id: str, agent_id: str, tenant_id: str, outcome: RunOutcome
) -> None:
    try:
        from ..plugins.manager import get_manager

        status_v = (
            outcome.status.value if hasattr(outcome.status, "value") else str(outcome.status)
        )
        for hook in get_manager().get_hooks():
            try:
                await hook.after_run(
                    run_id=run_id,
                    agent_id=agent_id,
                    tenant_id=tenant_id,
                    status=status_v,
                    summary=outcome.summary,
                )
            except Exception:
                log.exception("hook %s after_run raised", hook.name)
    except Exception:
        pass
