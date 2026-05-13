"""ORM models.

Four tables form the spine:

- agents: a configured agent (name, template, params, schedule, enabled).
- runs:   one execution of an agent. Long-lived; carries status + summary.
- events: append-only audit log of every step inside a run (tool call,
          model turn, decision, error). Replaying events reconstructs a run.
- approvals: human-in-the-loop gates that pause a run until decided.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class RunStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    awaiting_approval = "awaiting_approval"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"


class EventKind(str, enum.Enum):
    run_started = "run_started"
    model_turn = "model_turn"
    tool_call = "tool_call"
    tool_result = "tool_result"
    decision = "decision"
    approval_requested = "approval_requested"
    approval_resolved = "approval_resolved"
    approval_expired = "approval_expired"
    run_cancelled = "run_cancelled"
    run_finished = "run_finished"
    # Emitted by the orchestration layer once a Run reaches any terminal
    # state (succeeded / failed / cancelled). Subscribers that just want
    # "is this run done" should listen for this rather than reasoning
    # over the per-phase error/finished/cancelled events.
    run_terminal = "run_terminal"
    error = "error"


class ApprovalStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"


class Tenant(Base):
    """A tenant owns a set of Agents (and indirectly their Runs).

    `api_keys` is a JSON list of opaque tokens; the auth middleware resolves
    an inbound key against every tenant's list and stamps the request's
    tenant_id from the match. For dev / single-tenant deployments the
    bundled "default" tenant has no keys, which means anonymous access
    routes there.
    """

    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(160), default="")
    api_keys: Mapped[list] = mapped_column(JSON, default=list)
    # Quotas. 0 = unlimited (the dev-friendly default). Enforced at the
    # API layer: agent creation checks max_agents; trigger checks
    # max_runs_per_day in addition to the in-memory rate limit.
    max_agents: Mapped[int] = mapped_column(Integer, default=0)
    max_runs_per_day: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


DEFAULT_TENANT_NAME = "default"


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # Tenant ownership. Nullable for back-compat with pre-multi-tenant
    # databases — the bootstrap step backfills nulls to the default
    # tenant on startup so all in-memory code can assume non-null.
    tenant_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tenants.id"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(120), index=True)
    template: Mapped[str] = mapped_column(String(120))  # template key
    description: Mapped[str] = mapped_column(Text, default="")
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    schedule_cron: Mapped[str | None] = mapped_column(String(120), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    runs: Mapped[list["Run"]] = relationship(back_populates="agent", cascade="all, delete-orphan")

    # Names are unique per-tenant, not globally. The check is enforced in
    # the DB and re-checked in the API layer for friendlier error messages.
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="ux_agents_tenant_name"),)


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # Denormalized from agent.tenant_id for fast tenant filtering on the
    # runs list (avoids a join on every dashboard refresh).
    tenant_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), index=True)
    trigger: Mapped[str] = mapped_column(String(40))  # manual|cron|webhook|api
    trigger_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[RunStatus] = mapped_column(Enum(RunStatus), default=RunStatus.pending)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    tokens_input: Mapped[int] = mapped_column(Integer, default=0)
    tokens_output: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd_cents: Mapped[int] = mapped_column(Integer, default=0)  # x100 to keep ints

    # Serialized runtime state — populated only when the run is paused
    # mid-loop (awaiting_approval). On resume the runtime reads this back
    # to reconstruct the conversation, pending tool calls, and counters.
    state_json: Mapped[dict] = mapped_column(JSON, default=dict)

    agent: Mapped[Agent] = relationship(back_populates="runs")
    events: Mapped[list["Event"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="Event.created_at"
    )


class Event(Base):
    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    kind: Mapped[EventKind] = mapped_column(Enum(EventKind))
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    run: Mapped[Run] = relationship(back_populates="events")


class McpServer(Base):
    """A registered MCP server, persisted so it re-attaches across restarts.

    Tenant-scoped: each tenant has its own MCP servers, so an agent in
    tenant A can't accidentally call a tool registered by tenant B.
    """

    __tablename__ = "mcp_servers"

    name: Mapped[str] = mapped_column(String(120), primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tenants.id"), nullable=True, index=True
    )
    transport: Mapped[str] = mapped_column(String(16), default="stdio")
    # stdio: command + env. http: url + headers (kept under env JSON to
    # avoid a schema change just for one extra field — the lifespan
    # restorer disambiguates by transport).
    command: Mapped[list] = mapped_column(JSON, default=list)
    env: Mapped[dict] = mapped_column(JSON, default=dict)
    url: Mapped[str] = mapped_column(String(500), default="")
    headers: Mapped[dict] = mapped_column(JSON, default=dict)
    destructive_tools: Mapped[list] = mapped_column(JSON, default=list)
    description: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class PluginState(Base):
    """Persisted enable/disable state for plugins.

    The plugin manager upserts a row on first discovery, then consults
    `enabled` on every load. Operators flip rows via CLI / HTTP.

    `misconfig_reason` is set non-empty when the plugin declared
    required config fields that resolved to empty values — the
    manager keeps the plugin row but doesn't activate it, and surfaces
    the reason in `pilothouse plugins doctor`.
    """

    __tablename__ = "plugins"

    name: Mapped[str] = mapped_column(String(160), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[str] = mapped_column(String(40), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(160), default="")  # builtin|entry_point|directory:<path>
    kinds_json: Mapped[list] = mapped_column(JSON, default=list)
    misconfig_reason: Mapped[str] = mapped_column(Text, default="")
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class PluginConfig(Base):
    """Operator-set values for a plugin's config schema.

    One row per (plugin_name, key). Stored as plain JSON for simplicity;
    real production deployments encrypt at the volume / DB layer for
    secret-flagged values.
    """

    __tablename__ = "plugin_configs"

    plugin_name: Mapped[str] = mapped_column(String(160), primary_key=True)
    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # Denormalized from run.tenant_id so approval-list queries don't need
    # a two-hop join through runs+agents.
    tenant_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    # The exact tool_use the model emitted — we re-execute the same input
    # verbatim on approval, so an operator approves a specific action.
    tool_name: Mapped[str] = mapped_column(String(120))
    tool_use_id: Mapped[str] = mapped_column(String(120), default="")
    tool_input: Mapped[dict] = mapped_column(JSON, default=dict)
    rationale: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[ApprovalStatus] = mapped_column(
        Enum(ApprovalStatus), default=ApprovalStatus.pending
    )
    resolved_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    rejection_reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
