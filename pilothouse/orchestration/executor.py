"""Public orchestration surface — dispatcher across executor backends.

Pilothouse supports three deployment topologies, all behind the same
function signatures:

  * **In-process** (default) — same process as the caller, asyncio-driven.
    Set no env vars; you get this. Implementation: `_inprocess.py`.

  * **Temporal — dev mode** — `PILOTHOUSE_TEMPORAL_ADDRESS=dev`. Boots a
    Temporal dev server inside the same process; workflows are durable,
    no external infra required. Useful for staging on one box.

  * **Temporal — distributed** — `PILOTHOUSE_TEMPORAL_ADDRESS=<host:port>`
    points at a real Temporal cluster. Worker can be scaled
    horizontally; workflows survive process restarts. Implementation:
    `_temporal.py`.

Callers never construct executors directly — they call the
module-level functions below, which dispatch via `_get_executor()`.
This is the seam that lets Temporal be entirely optional: when the
in-process executor is selected (the default), `temporalio` doesn't
even need to be importable.

The legacy `RunExecutor` class is preserved as a thin shim for any
external code that imported it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from ..agent.runtime import RunOutcome
from ..config import get_settings
from ..db import session
from ..events import RunEvent, get_bus
from ..models import Approval, ApprovalStatus, Event, EventKind, Run, RunStatus
from ._common import (
    PRICE_IN_PER_M_USD,
    PRICE_OUT_PER_M_USD,
    fire_hooks_after,
    fire_hooks_before,
    persist_outcome,
)
from ._inprocess import InProcessExecutor

log = logging.getLogger(__name__)


# --- dispatcher --------------------------------------------------------

# Cached so the (small) cost of constructing the executor + setting up
# the Temporal client doesn't repeat on every call. Process-wide; reset
# via `reset_executor()` for tests.
_cached_executor = None


def _get_executor():
    global _cached_executor
    if _cached_executor is not None:
        return _cached_executor
    settings = get_settings()
    addr = (settings.temporal_address or "").strip()
    if addr:
        # Lazy import — temporalio is an optional dependency.
        try:
            from ._temporal import TemporalExecutor
        except ImportError as exc:
            raise RuntimeError(
                "PILOTHOUSE_TEMPORAL_ADDRESS is set but the temporalio package "
                "is not installed. Run `pip install 'pilothouse[temporal]'`."
            ) from exc
        _cached_executor = TemporalExecutor(
            address=addr,
            namespace=settings.temporal_namespace,
            task_queue=settings.temporal_task_queue,
        )
    else:
        _cached_executor = InProcessExecutor()
    return _cached_executor


def reset_executor() -> None:
    """Test helper — drop the cached executor."""
    global _cached_executor
    _cached_executor = None


def executor_kind() -> str:
    """Used by `pilothouse temporal status` and the console for display."""
    e = _get_executor()
    name = type(e).__name__
    if name == "TemporalExecutor":
        return f"temporal({getattr(e, 'address', '?')})"
    return "inprocess"


# --- public API --------------------------------------------------------


class RunExecutor:
    """Legacy wrapper — old callers used `RunExecutor().execute(...)`.

    New code should call the module-level functions directly; this is
    kept for back-compat with any out-of-tree callers.
    """

    async def execute(
        self,
        *,
        agent_id: str,
        trigger: str,
        trigger_payload: dict,
        dry_run_override: bool | None = None,
    ) -> str:
        return await execute_agent(
            agent_id=agent_id,
            trigger=trigger,
            trigger_payload=trigger_payload,
            dry_run_override=dry_run_override,
        )


async def execute_agent(
    *,
    agent_id: str,
    trigger: str,
    trigger_payload: dict,
    dry_run_override: bool | None = None,
) -> str:
    """Start a fresh agent run. Returns the run_id.

    The run may complete (status=succeeded/failed), pause
    (status=awaiting_approval), or be cancelled. Either way the row is
    persisted before this function returns.
    """
    return await _get_executor().execute_agent(
        agent_id=agent_id,
        trigger=trigger,
        trigger_payload=trigger_payload,
        dry_run_override=dry_run_override,
    )


async def resume_run(run_id: str) -> RunOutcome:
    """Resume a Run paused at an approval gate. Caller must verify
    every Approval has been resolved; the runtime itself refuses to
    proceed if any are still pending."""
    return await _get_executor().resume_run(run_id)


async def retry_run(run_id: str, *, dry_run_override: bool | None = None) -> str:
    """Re-execute an existing run's trigger payload as a fresh run."""
    return await _get_executor().retry_run(run_id, dry_run_override=dry_run_override)


async def cancel_run(run_id: str, *, by: str = "operator") -> Run:
    """Signal cancellation. The mechanism differs by executor —
    in-process flips a DB status flag, Temporal sends a workflow
    signal — but the outcome (Run.status=cancelled + run_cancelled
    event) is the same."""
    return await _get_executor().cancel_run(run_id, by=by)


# --- shared (executor-independent) -------------------------------------


async def sweep_expired_approvals() -> int:
    """Auto-reject Approvals older than `approval_ttl_minutes`.

    Returns the number of approvals expired. After expiry the run is
    resumed automatically via the configured executor when no other
    pending approval remains.
    """
    settings = get_settings()
    cutoff = datetime.now(timezone.utc) - timedelta(
        minutes=settings.approval_ttl_minutes
    )
    expired_runs: set[str] = set()
    expired_count = 0
    async with session() as s:
        stale = (
            await s.execute(
                select(Approval).where(
                    Approval.status == ApprovalStatus.pending,
                    Approval.created_at < cutoff,
                )
            )
        ).scalars().all()
        now = datetime.now(timezone.utc)
        for a in stale:
            a.status = ApprovalStatus.rejected
            a.resolved_by = "system:ttl"
            a.rejection_reason = (
                f"Approval expired after {settings.approval_ttl_minutes} minutes."
            )
            a.resolved_at = now
            ev = Event(
                run_id=a.run_id,
                kind=EventKind.approval_expired,
                data={
                    "approval_id": a.id,
                    "tool": a.tool_name,
                    "ttl_minutes": settings.approval_ttl_minutes,
                },
            )
            s.add(ev)
            get_bus().publish(
                RunEvent(
                    run_id=a.run_id,
                    kind=EventKind.approval_expired.value,
                    data={"approval_id": a.id, "tool": a.tool_name},
                )
            )
            expired_runs.add(a.run_id)
            expired_count += 1

    # Resume any runs whose pending count just dropped to zero. Goes
    # through the dispatcher so Temporal mode resumes via signal.
    for run_id in expired_runs:
        async with session() as s:
            remaining = (
                await s.execute(
                    select(Approval).where(
                        Approval.run_id == run_id,
                        Approval.status == ApprovalStatus.pending,
                    )
                )
            ).scalars().first()
        if remaining is None:
            try:
                await resume_run(run_id)
            except Exception as exc:  # pragma: no cover — logged for ops
                log.exception(
                    "auto-resume after TTL expiry failed for %s: %s", run_id, exc
                )
    return expired_count


__all__ = [
    "RunExecutor",
    "InProcessExecutor",
    "execute_agent",
    "resume_run",
    "retry_run",
    "cancel_run",
    "sweep_expired_approvals",
    "executor_kind",
    "reset_executor",
    "fire_hooks_before",
    "fire_hooks_after",
    "persist_outcome",
    "PRICE_IN_PER_M_USD",
    "PRICE_OUT_PER_M_USD",
]
