"""Agent runtime — tool-calling loop with pause/resume on approval.

Lifecycle:

  start_run(...)                    →  RunOutcome(status in {succeeded, failed, awaiting_approval})
  resume_run(run_id)                →  RunOutcome(...)

The runtime treats every model turn as a checkpoint. When the assistant
issues tool_use blocks the runtime walks them in order:

  * Non-destructive tools (or destructive ones in dry-run) execute
    immediately and produce real tool_result blocks.
  * Destructive tools — when `require_approval_for_writes` is set AND the
    run is not in dry-run — create Approval rows. Execution is deferred:
    the runtime persists messages-so-far + the resolved tool_results from
    this turn + the list of pending approvals, sets the Run to
    awaiting_approval, and exits.

`resume_run` reads the persisted state back, asks the DB whether each
Approval is now approved or rejected, executes / rejects each pending tool
call accordingly, builds the full tool_results turn and resumes the loop.

A second safety: even with no approval policy, every destructive tool
invocation is still routed through dry-run when the agent is in dry-run.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..connectors.base import Tool, ToolContext, ToolResult, as_text, registry
from ..events import RunEvent, get_bus
from ..models import Approval, ApprovalStatus, Event, EventKind, Run, RunStatus
from .providers import get_provider

log = logging.getLogger(__name__)


@dataclass
class RunOutcome:
    status: RunStatus
    summary: str
    tokens_input: int = 0
    tokens_output: int = 0
    iterations: int = 0
    tool_calls: int = 0
    pending_approval_ids: list[str] = field(default_factory=list)
    extras: dict = field(default_factory=dict)


class AgentRunner:
    """Stateless helper around one Run. Constructed per call with a session."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # --- public entry points ---------------------------------------------

    async def start(
        self,
        *,
        run: Run,
        system_prompt: str,
        user_message: str,
        tools: list[Tool],
        dry_run: bool,
        require_approval_for_writes: bool,
        params: dict,
        agent_id: str,
    ) -> RunOutcome:
        """Begin a fresh run. May complete, fail, or pause for approval."""
        await self._emit(
            run.id,
            EventKind.run_started,
            {
                "dry_run": dry_run,
                "require_approval_for_writes": require_approval_for_writes,
                "tool_names": [t.name for t in tools],
            },
        )
        state = {
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_message}],
            "tool_names": [t.name for t in tools],
            "iterations": 0,
            "tokens_in": 0,
            "tokens_out": 0,
            "tool_calls": 0,
            "dry_run": dry_run,
            "require_approval_for_writes": require_approval_for_writes,
            "params": params,
            "agent_id": agent_id,
        }
        return await self._drive(run=run, state=state, tools=tools)

    async def resume(self, *, run: Run, tools: list[Tool]) -> RunOutcome:
        """Resume a run previously paused at an approval gate."""
        state = dict(run.state_json or {})
        if not state:
            return RunOutcome(
                status=RunStatus.failed,
                summary="Cannot resume — run has no persisted state.",
            )
        # Resolve pending approvals → tool_results blocks
        results_block = list(state.get("partial_tool_results", []))
        pending = list(state.get("pending_approvals", []))

        approvals_by_id: dict[str, Approval] = {}
        if pending:
            ids = [p["approval_id"] for p in pending]
            rows = (
                await self.session.execute(select(Approval).where(Approval.id.in_(ids)))
            ).scalars().all()
            approvals_by_id = {a.id: a for a in rows}

            # All approvals must be resolved before we can move forward.
            still_pending = [a for a in rows if a.status == ApprovalStatus.pending]
            if still_pending:
                return RunOutcome(
                    status=RunStatus.awaiting_approval,
                    summary="Still awaiting approvals.",
                    tokens_input=state.get("tokens_in", 0),
                    tokens_output=state.get("tokens_out", 0),
                    iterations=state.get("iterations", 0),
                    pending_approval_ids=[a.id for a in still_pending],
                )

        # Build tool_results for each pending entry, in original order.
        tools_by_name = {t.name: t for t in tools}
        for entry in pending:
            ap = approvals_by_id[entry["approval_id"]]
            tool_use_id = entry["tool_use_id"]
            tool_name = entry["tool_name"]
            tool_input = entry["tool_input"]

            if ap.status == ApprovalStatus.rejected:
                await self._emit(
                    run.id,
                    EventKind.approval_resolved,
                    {
                        "approval_id": ap.id,
                        "tool": tool_name,
                        "decision": "rejected",
                        "by": ap.resolved_by,
                        "reason": ap.rejection_reason,
                    },
                )
                rejection = {
                    "rejected_by_operator": True,
                    "tool": tool_name,
                    "reason": ap.rejection_reason or "operator rejected this action",
                    "approver": ap.resolved_by,
                }
                results_block.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": as_text(rejection),
                        "is_error": False,
                    }
                )
                continue

            # approved → actually execute
            await self._emit(
                run.id,
                EventKind.approval_resolved,
                {
                    "approval_id": ap.id,
                    "tool": tool_name,
                    "decision": "approved",
                    "by": ap.resolved_by,
                },
            )
            tool = tools_by_name.get(tool_name)
            if tool is None:
                result = ToolResult(
                    content={"error": f"tool {tool_name} not found at resume"},
                    is_error=True,
                )
            else:
                try:
                    result = await tool.handler(
                        self._make_ctx(run.id, state, dry_run=False), tool_input
                    )
                except Exception as exc:
                    result = ToolResult(content={"error": str(exc)}, is_error=True)
            await self._emit(
                run.id,
                EventKind.tool_result,
                {
                    "tool": tool_name,
                    "tool_use_id": tool_use_id,
                    "is_error": result.is_error,
                    "preview": _preview(result.content),
                    "post_approval": True,
                },
            )
            results_block.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": as_text(result.content),
                    "is_error": result.is_error,
                }
            )

        # Append all tool_results as a single user turn and clear gates.
        messages = list(state["messages"])
        messages.append({"role": "user", "content": results_block})
        state["messages"] = messages
        state.pop("partial_tool_results", None)
        state.pop("pending_approvals", None)

        return await self._drive(run=run, state=state, tools=tools)

    # --- core loop -------------------------------------------------------

    async def _drive(self, *, run: Run, state: dict, tools: list[Tool]) -> RunOutcome:
        settings = get_settings()
        provider = get_provider(settings)
        tools_schema = [t.to_anthropic() for t in tools]
        tools_by_name = {t.name: t for t in tools}

        messages: list[dict[str, Any]] = list(state["messages"])
        total_in = int(state.get("tokens_in", 0))
        total_out = int(state.get("tokens_out", 0))
        iters = int(state.get("iterations", 0))
        tool_calls = int(state.get("tool_calls", 0))
        dry_run = bool(state["dry_run"])
        gate_writes = bool(state["require_approval_for_writes"])
        params = dict(state.get("params") or {})
        agent_id = state.get("agent_id", "")

        final_text = ""

        while iters < settings.max_tool_iterations:
            iters += 1
            # Cooperative cancellation: another writer (HTTP cancel,
            # CLI cancel) sets Run.status=cancelled. We notice between
            # iterations and exit cleanly with the audit trail intact.
            await self.session.refresh(run, attribute_names=["status"])
            if run.status == RunStatus.cancelled:
                await self._emit(
                    run.id,
                    EventKind.run_cancelled,
                    {"iteration": iters, "tokens_input": total_in, "tokens_output": total_out},
                )
                return RunOutcome(
                    status=RunStatus.cancelled,
                    summary="Run cancelled by operator.",
                    tokens_input=total_in,
                    tokens_output=total_out,
                    iterations=iters,
                    tool_calls=tool_calls,
                )
            try:
                response = await provider.complete(
                    system=state["system"],
                    messages=messages,
                    tools=tools_schema,
                    max_tokens=settings.max_output_tokens,
                    model=settings.model_planner,
                )
            except Exception as exc:
                await self._emit(
                    run.id, EventKind.error, {"phase": "model_call", "error": str(exc)}
                )
                return RunOutcome(
                    status=RunStatus.failed,
                    summary=f"Model call failed: {exc}",
                    tokens_input=total_in,
                    tokens_output=total_out,
                    iterations=iters,
                    tool_calls=tool_calls,
                )

            usage = response.get("usage", {})
            total_in += int(usage.get("input_tokens", 0))
            total_out += int(usage.get("output_tokens", 0))
            blocks: list[dict[str, Any]] = response.get("content", []) or []

            await self._emit(
                run.id,
                EventKind.model_turn,
                {
                    "iteration": iters,
                    "stop_reason": response.get("stop_reason"),
                    "blocks": _summarize_blocks(blocks),
                    "usage": usage,
                },
            )
            messages.append({"role": "assistant", "content": blocks})

            tool_uses = [b for b in blocks if b.get("type") == "tool_use"]
            if not tool_uses:
                final_text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")
                break

            # Walk tool_uses in order. Non-destructive (or dry-run / no-gate)
            # tools execute now. Destructive tools needing approval get an
            # Approval row and are deferred.
            partial_results: list[dict[str, Any]] = []
            pending: list[dict[str, Any]] = []

            for tu in tool_uses:
                tool_calls += 1
                name = tu.get("name", "")
                tool_input = tu.get("input", {}) or {}
                tool_use_id = tu.get("id", "")
                tool = tools_by_name.get(name)
                await self._emit(
                    run.id,
                    EventKind.tool_call,
                    {"tool": name, "input": tool_input, "tool_use_id": tool_use_id},
                )

                needs_approval = (
                    tool is not None
                    and tool.is_destructive
                    and gate_writes
                    and not dry_run
                )

                if needs_approval:
                    rationale = _rationale_from_blocks(blocks)
                    ap = Approval(
                        run_id=run.id,
                        tenant_id=run.tenant_id,
                        tool_name=name,
                        tool_use_id=tool_use_id,
                        tool_input=tool_input,
                        rationale=rationale,
                    )
                    self.session.add(ap)
                    await self.session.flush()
                    # Include all data the notifier needs in the event
                    # payload so it doesn't have to read the not-yet-
                    # committed Approval row from a fresh DB session.
                    await self._emit(
                        run.id,
                        EventKind.approval_requested,
                        {
                            "approval_id": ap.id,
                            "tool": name,
                            "input": tool_input,
                            "rationale": rationale,
                            "agent_id": agent_id,
                            "tenant_id": run.tenant_id,
                            "params": dict(state.get("params") or {}),
                        },
                    )
                    pending.append(
                        {
                            "approval_id": ap.id,
                            "tool_use_id": tool_use_id,
                            "tool_name": name,
                            "tool_input": tool_input,
                        }
                    )
                    continue

                if tool is None:
                    result = ToolResult(
                        content={"error": f"unknown tool: {name}"}, is_error=True
                    )
                elif tool.is_destructive and dry_run:
                    result = ToolResult(
                        content={
                            "dry_run": True,
                            "tool": name,
                            "would_have_called_with": tool_input,
                            "note": "destructive tool short-circuited by runtime dry-run",
                        }
                    )
                else:
                    try:
                        result = await tool.handler(
                            self._make_ctx(run.id, state, dry_run=dry_run), tool_input
                        )
                    except Exception as exc:
                        result = ToolResult(content={"error": str(exc)}, is_error=True)
                await self._emit(
                    run.id,
                    EventKind.tool_result,
                    {
                        "tool": name,
                        "tool_use_id": tool_use_id,
                        "is_error": result.is_error,
                        "preview": _preview(result.content),
                    },
                )
                partial_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": as_text(result.content),
                        "is_error": result.is_error,
                    }
                )

            if pending:
                # Persist checkpoint and pause.
                state["messages"] = messages
                state["partial_tool_results"] = partial_results
                state["pending_approvals"] = pending
                state["tokens_in"] = total_in
                state["tokens_out"] = total_out
                state["iterations"] = iters
                state["tool_calls"] = tool_calls
                run.state_json = _safe_json(state)
                await self.session.flush()
                return RunOutcome(
                    status=RunStatus.awaiting_approval,
                    summary=f"Paused: {len(pending)} approval(s) required for destructive tool calls.",
                    tokens_input=total_in,
                    tokens_output=total_out,
                    iterations=iters,
                    tool_calls=tool_calls,
                    pending_approval_ids=[p["approval_id"] for p in pending],
                )

            # No deferred work — feed the results back as the next user turn.
            messages.append({"role": "user", "content": partial_results})
        else:
            await self._emit(
                run.id,
                EventKind.error,
                {"phase": "loop", "error": "max_tool_iterations exceeded"},
            )
            return RunOutcome(
                status=RunStatus.failed,
                summary=f"Stopped after {iters} iterations without a final answer.",
                tokens_input=total_in,
                tokens_output=total_out,
                iterations=iters,
                tool_calls=tool_calls,
            )

        await self._emit(
            run.id,
            EventKind.run_finished,
            {"tokens_input": total_in, "tokens_output": total_out, "iterations": iters},
        )
        # Successful completion clears the checkpoint.
        run.state_json = {}
        return RunOutcome(
            status=RunStatus.succeeded,
            summary=final_text or "(no final text)",
            tokens_input=total_in,
            tokens_output=total_out,
            iterations=iters,
            tool_calls=tool_calls,
        )

    # --- helpers ---------------------------------------------------------

    async def _emit(self, run_id: str, kind: EventKind, data: dict) -> None:
        safe = _safe_json(data)
        ev = Event(run_id=run_id, kind=kind, data=safe)
        self.session.add(ev)
        await self.session.flush()
        kind_value = kind.value if hasattr(kind, "value") else str(kind)
        get_bus().publish(RunEvent(run_id=run_id, kind=kind_value, data=safe))

    def _make_ctx(self, run_id: str, state: dict, *, dry_run: bool) -> ToolContext:
        async def _emit_for_run(name: str, data: dict) -> None:
            await self._emit(run_id, EventKind.decision, {"label": name, **data})

        return ToolContext(
            run_id=run_id,
            agent_id=state.get("agent_id", ""),
            dry_run=dry_run,
            params=dict(state.get("params") or {}),
            emit=_emit_for_run,
        )


# --- helpers --------------------------------------------------------------


def _safe_json(d: dict) -> dict:
    return json.loads(json.dumps(d, default=str))


def _summarize_blocks(blocks: list[dict]) -> list[dict]:
    out: list[dict] = []
    for b in blocks:
        t = b.get("type")
        if t == "text":
            out.append({"type": "text", "text_preview": (b.get("text") or "")[:200]})
        elif t == "tool_use":
            out.append({"type": "tool_use", "name": b.get("name"), "input": b.get("input")})
        else:
            out.append({"type": t})
    return out


def _preview(content: Any, limit: int = 500) -> str:
    text = as_text(content)
    if len(text) > limit:
        return text[:limit] + f"… ({len(text) - limit} bytes truncated)"
    return text


def _rationale_from_blocks(blocks: list[dict]) -> str:
    """Extract the assistant's explanatory text from a turn so reviewers
    know *why* a destructive action was proposed."""
    texts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
    rationale = "\n\n".join(t.strip() for t in texts if t and t.strip())
    return rationale[:2000]


__all__ = ["AgentRunner", "RunOutcome"]
