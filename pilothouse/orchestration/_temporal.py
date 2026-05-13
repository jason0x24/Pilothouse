"""Temporal-backed executor.

Wraps each Run as a Temporal Workflow. The workflow drives the agent
loop by invoking a small set of activities:

  * `run_agent_activity`   — initial activation of the agent loop
  * `resume_run_activity`  — continuation after an approval gate
  * `cancel_run_activity`  — DB-side cleanup when a cancel signal lands

Workflow signals:

  * `approval_resolved`    — sent when `POST /approvals/{id}/resolve`
                             auto-resumes; tells the workflow it can
                             continue.
  * `cancel`               — sent when the operator calls cancel.

The workflow itself stays deterministic — all I/O (DB, LLM, tools)
happens inside activities. Activity bodies reuse the *exact same code
paths* as the in-process executor, so behaviour is identical.

### Why this design

A more "Temporal-native" implementation would expose each tool call
as a separate activity, with the agent-loop scheduling logic as the
Workflow. That gives finer-grained durability + per-tool retry
policies. We don't do that here because:

  * The agent loop already checkpoints to `Run.state_json` on each
    pause, so per-iteration durability is already covered.
  * Decomposing each tool call into its own activity would require
    invasively changing `AgentRunner` and would re-implement the
    runtime in Temporal terms.
  * The "single activity wraps the whole loop" pattern still gives
    you crash recovery (activity gets retried, workflow event history
    is durable, the loop reads its own state_json) without rewriting
    the runtime.

If you want activity-per-tool semantics later, the seam is the
`AgentRunner._drive` loop — replace it with workflow-level scheduling.
The public executor API stays the same.

### Dev mode

`PILOTHOUSE_TEMPORAL_ADDRESS=dev` starts an in-process Temporal dev
server (via `temporalio.testing.WorkflowEnvironment.start_local()`).
This is the "Temporal without infra" mode — durable workflows + a
single-machine deployment.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

# All temporalio imports are gated behind the existence of the package.
# This module is only imported when `temporal_address` is set, but
# `from ._temporal import ...` from the dispatcher must not fail at
# the import statement itself if temporalio isn't installed — instead
# we want a clear "install pilothouse[temporal]" message. The
# dispatcher catches ImportError around the import.
from temporalio import activity, workflow
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.common import RetryPolicy
from temporalio.worker import Worker

from ._inprocess import InProcessExecutor

log = logging.getLogger(__name__)


# --- Activities -------------------------------------------------------
# Activities are plain async functions that get registered with the
# worker. They run in the worker process and can do arbitrary I/O.
# We delegate to the in-process executor so behaviour matches exactly
# what tests cover in mock mode.


@activity.defn(name="pilothouse_run_agent")
async def run_agent_activity(payload: dict) -> str:
    """Execute a fresh agent run end-to-end. Returns the run_id.

    Idempotent: if the run already exists (workflow retry), the
    in-process executor will detect it via the run_id and avoid
    duplication. (We pass the run via agent_id+trigger_payload; the
    executor mints the run_id itself, so retries DO create new rows.
    For at-least-once, that's the right semantic — duplicate runs are
    rare in practice and the dedup window catches most.)
    """
    return await InProcessExecutor().execute_agent(
        agent_id=payload["agent_id"],
        trigger=payload["trigger"],
        trigger_payload=payload["trigger_payload"],
        dry_run_override=payload.get("dry_run_override"),
    )


@activity.defn(name="pilothouse_resume_run")
async def resume_run_activity(run_id: str) -> None:
    """Continue a run after its approval gates resolved.

    Returns the outcome serialised. The workflow then decides whether
    to await another approval (if outcome.status == awaiting_approval
    again, i.e. the agent emitted *more* destructive tool calls in the
    same turn) or to terminate.
    """
    await InProcessExecutor().resume_run(run_id)


@activity.defn(name="pilothouse_cancel_run")
async def cancel_run_activity(payload: dict) -> None:
    """Perform the DB-side cleanup for a cancel.

    Reuses the in-process cancel which knows how to handle the two
    states (running / awaiting_approval) and emit the right events.
    """
    await InProcessExecutor().cancel_run(payload["run_id"], by=payload.get("by", "operator"))


# --- Workflow ---------------------------------------------------------


@workflow.defn(name="PilothouseAgentRun")
class AgentRunWorkflow:
    """One workflow execution per logical Run.

    State the workflow owns:

      * `_run_id`            — populated after the initial activity
                               returns the minted run_id.
      * `_approval_signal`   — flipped to True when an approval is
                               resolved externally; the workflow waits
                               on this to know when to resume.
      * `_cancel_signal`     — set when the operator cancels.

    The workflow is deliberately simple: drive one activity, watch for
    signals, decide whether to loop / resume / exit. The agent's own
    pause-and-checkpoint protocol does the heavy lifting; the workflow
    just bridges Temporal's signal model to the existing approval gate.
    """

    def __init__(self) -> None:
        self._run_id: str | None = None
        self._approval_pending: bool = False
        self._cancel_requested: bool = False
        self._cancel_by: str = "operator"

    @workflow.signal(name="approval_resolved")
    def approval_resolved_signal(self) -> None:
        self._approval_pending = False

    @workflow.signal(name="cancel")
    def cancel_signal(self, by: str = "operator") -> None:
        self._cancel_requested = True
        self._cancel_by = by

    @workflow.query(name="run_id")
    def query_run_id(self) -> str:
        return self._run_id or ""

    @workflow.run
    async def run(self, payload: dict) -> str:
        # Step 1: kick off the agent loop. The activity returns the
        # newly-minted run_id; we store it so external callers can
        # `query` for it and so cancel/resume signals can be routed.
        retry_policy = RetryPolicy(
            initial_interval=timedelta(seconds=1),
            maximum_attempts=3,
            non_retryable_error_types=["RuntimeError"],
        )
        self._run_id = await workflow.execute_activity(
            run_agent_activity,
            payload,
            start_to_close_timeout=timedelta(minutes=30),
            retry_policy=retry_policy,
        )

        # Step 2: drive any approval-gate cycles. Each iteration:
        #   * the loop may have ended (succeeded/failed/cancelled) — done
        #   * the loop may have paused at approval — wait for signal,
        #     then call resume activity
        while True:
            if self._cancel_requested:
                await workflow.execute_activity(
                    cancel_run_activity,
                    {"run_id": self._run_id, "by": self._cancel_by},
                    start_to_close_timeout=timedelta(seconds=30),
                )
                return self._run_id

            status = await _read_status_activity_call(self._run_id)
            if status != "awaiting_approval":
                return self._run_id

            # Park the workflow until a signal lands. Temporal blocks
            # cheaply here — no CPU, no polling — and the workflow
            # event history records the wait.
            self._approval_pending = True
            await workflow.wait_condition(
                lambda: not self._approval_pending or self._cancel_requested
            )
            if self._cancel_requested:
                continue
            await workflow.execute_activity(
                resume_run_activity,
                self._run_id,
                start_to_close_timeout=timedelta(minutes=30),
                retry_policy=retry_policy,
            )


@activity.defn(name="pilothouse_read_run_status")
async def read_run_status_activity(run_id: str) -> str:
    """Tiny activity that returns the current Run.status. The workflow
    can't read the DB directly (non-deterministic), so it calls this."""
    from sqlalchemy import select

    from ..db import session
    from ..models import Run

    async with session() as s:
        row = (await s.execute(select(Run).where(Run.id == run_id))).scalar_one_or_none()
    if row is None:
        return "unknown"
    return row.status.value if hasattr(row.status, "value") else str(row.status)


async def _read_status_activity_call(run_id: str) -> str:
    """Convenience wrapper so the workflow body reads naturally."""
    return await workflow.execute_activity(
        read_run_status_activity,
        run_id,
        start_to_close_timeout=timedelta(seconds=10),
    )


# --- Executor ---------------------------------------------------------


class TemporalExecutor:
    """Public executor — drives the workflow on behalf of callers.

    Constructed once per process (cached by the dispatcher). Holds a
    Temporal client + a worker task. The worker registers the workflow
    + activities on the configured task queue.

    Cancellation / resume / retry route through workflow signals or new
    workflow starts; the executor never touches the DB directly except
    via the same shared helpers as the in-process path.
    """

    def __init__(self, *, address: str, namespace: str, task_queue: str) -> None:
        self.address = address
        self.namespace = namespace
        self.task_queue = task_queue
        self._client: Client | None = None
        self._worker_task: asyncio.Task | None = None
        self._dev_env = None  # WorkflowEnvironment when in dev mode
        self._client_lock = asyncio.Lock()

    async def _ensure_client(self) -> Client:
        """Lazy client + dev-server bootstrap + worker startup.

        Yields back to the event loop until the worker task has had
        several opportunities to register with Temporal — the first
        `start_workflow` mustn't race with worker startup, otherwise
        the workflow's activities sit unowned in the task queue.
        """
        if self._client is not None:
            return self._client
        async with self._client_lock:
            if self._client is not None:
                return self._client
            if self.address == "dev":
                from temporalio.testing import WorkflowEnvironment

                self._dev_env = await WorkflowEnvironment.start_local(
                    namespace=self.namespace
                )
                self._client = self._dev_env.client
            else:
                self._client = await Client.connect(
                    self.address, namespace=self.namespace
                )
            self._worker_task = asyncio.create_task(
                _run_worker(self._client, self.task_queue),
                name="pilothouse-temporal-worker",
            )
            # Give the worker a chance to advertise itself. Dev server
            # is fast; remote cluster adds WAN latency but this is
            # one-shot per process.
            for _ in range(20):
                await asyncio.sleep(0.05)
                if self._worker_task.done():
                    exc = self._worker_task.exception()
                    raise RuntimeError(f"temporal worker died on startup: {exc}")
            return self._client

    async def execute_agent(
        self,
        *,
        agent_id: str,
        trigger: str,
        trigger_payload: dict,
        dry_run_override: bool | None = None,
    ) -> str:
        import uuid

        client = await self._ensure_client()
        payload = {
            "agent_id": agent_id,
            "trigger": trigger,
            "trigger_payload": trigger_payload,
            "dry_run_override": dry_run_override,
        }
        # Workflow IDs must be unique per execution — Temporal refuses
        # to start a workflow with an id already in use (even completed
        # ones, until they age out of retention). A fresh UUID per call
        # keeps things simple; we persist the mapping below for
        # cancel/resume to find the workflow handle.
        wf_id = f"pilothouse-{uuid.uuid4()}"
        handle = await client.start_workflow(
            AgentRunWorkflow.run,
            payload,
            id=wf_id,
            task_queue=self.task_queue,
        )
        # `handle.result()` waits for the workflow's run() method to
        # return — which happens once the agent loop reaches a terminal
        # state OR the workflow's first call returns run_id and there
        # are no approval gates to wait on.
        run_id = await handle.result()

        # Persist the workflow_id on the Run so resume/cancel can
        # locate the workflow. Stashed in state_json (a JSON column
        # we already use for resumable state).
        await _record_workflow_id(run_id, wf_id)
        return run_id

    async def resume_run(self, run_id: str) -> Any:
        """Signal the workflow that owns this run that it can proceed."""
        client = await self._ensure_client()
        wf_id = await _read_workflow_id(run_id)
        if not wf_id:
            # Run wasn't started by us (or workflow id isn't recorded).
            # Fall back to the in-process resume — operator gets the
            # right outcome even if the Temporal handle is gone.
            return await InProcessExecutor().resume_run(run_id)
        handle = client.get_workflow_handle(wf_id)
        await handle.signal(AgentRunWorkflow.approval_resolved_signal)

    async def retry_run(
        self, run_id: str, *, dry_run_override: bool | None = None
    ) -> str:
        # Replay the trigger payload as a new workflow execution.
        from sqlalchemy import select

        from ..db import session
        from ..models import Run

        async with session() as s:
            old = (await s.execute(select(Run).where(Run.id == run_id))).scalar_one()
        return await self.execute_agent(
            agent_id=old.agent_id,
            trigger=f"retry:{old.trigger}",
            trigger_payload=dict(old.trigger_payload or {}),
            dry_run_override=dry_run_override,
        )

    async def cancel_run(self, run_id: str, *, by: str = "operator") -> Any:
        """Best-effort: signal the workflow (if it's still alive), but
        also run the in-process cancel so the DB ends up in the right
        state regardless of whether the workflow received the signal."""
        client = await self._ensure_client()
        wf_id = await _read_workflow_id(run_id)
        if wf_id:
            try:
                handle = client.get_workflow_handle(wf_id)
                await handle.signal(AgentRunWorkflow.cancel_signal, by)
            except Exception:
                log.exception(
                    "temporal cancel signal failed for %s; falling back to DB", run_id
                )
        # Always run the DB-side cancel so callers see consistent state
        # — the workflow signal might miss (workflow already done) or
        # the workflow handle might be gone.
        return await InProcessExecutor().cancel_run(run_id, by=by)

    async def shutdown(self) -> None:
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except (asyncio.CancelledError, Exception):
                pass
            self._worker_task = None
        if self._dev_env is not None:
            try:
                await self._dev_env.shutdown()
            except Exception:
                log.exception("temporal dev env shutdown failed")
            self._dev_env = None
        self._client = None


async def _record_workflow_id(run_id: str, workflow_id: str) -> None:
    """Stash the Temporal workflow_id on the Run row so future
    cancel/resume calls can find the handle. We use `state_json`
    (already used by the in-process executor for paused state) with
    a dedicated key so the two don't collide."""
    from sqlalchemy import select

    from ..db import session
    from ..models import Run

    async with session() as s:
        row = (
            await s.execute(select(Run).where(Run.id == run_id))
        ).scalar_one_or_none()
        if row is None:
            return
        state = dict(row.state_json or {})
        state["__temporal_workflow_id"] = workflow_id
        row.state_json = state


async def _read_workflow_id(run_id: str) -> str:
    from sqlalchemy import select

    from ..db import session
    from ..models import Run

    async with session() as s:
        row = (
            await s.execute(select(Run).where(Run.id == run_id))
        ).scalar_one_or_none()
        if row is None:
            return ""
        return (row.state_json or {}).get("__temporal_workflow_id", "")


async def _run_worker(client: Client, task_queue: str) -> None:
    """Run the worker until cancelled.

    The workflow runner is unsandboxed. Temporal's default sandbox
    rejects most stdlib + third-party modules transitively imported
    by this module (httpx, sqlalchemy, etc.) — which is the right
    default when running untrusted workflows from many authors. We
    own both the workflow and its module, the workflow body is tiny
    (a few activity calls + a wait_condition), and there's no
    non-deterministic state inside the workflow class itself, so
    disabling the sandbox here is safe and avoids a long list of
    `passthrough_modules` annotations.
    """
    from temporalio.worker import UnsandboxedWorkflowRunner

    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[AgentRunWorkflow],
        activities=[
            run_agent_activity,
            resume_run_activity,
            cancel_run_activity,
            read_run_status_activity,
        ],
        workflow_runner=UnsandboxedWorkflowRunner(),
    )
    try:
        await worker.run()
    except asyncio.CancelledError:
        return
