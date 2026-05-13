"""Pydantic schemas for the HTTP layer."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class AgentCreate(BaseModel):
    name: str
    template: str
    description: str = ""
    params: dict = Field(default_factory=dict)
    schedule_cron: str | None = None
    enabled: bool = True
    dry_run: bool = True


class AgentUpdate(BaseModel):
    description: str | None = None
    params: dict | None = None
    schedule_cron: str | None = None
    enabled: bool | None = None
    dry_run: bool | None = None


class AgentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str | None
    name: str
    template: str
    description: str
    params: dict
    schedule_cron: str | None
    enabled: bool
    dry_run: bool
    created_at: datetime
    updated_at: datetime


class TenantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    display_name: str
    api_key_count: int
    created_at: datetime

    @classmethod
    def from_model(cls, t) -> "TenantOut":
        return cls(
            id=t.id,
            name=t.name,
            display_name=t.display_name,
            api_key_count=len(t.api_keys or []),
            created_at=t.created_at,
        )


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str | None
    agent_id: str
    trigger: str
    status: str
    summary: str
    tokens_input: int
    tokens_output: int
    cost_usd_cents: int
    started_at: datetime
    finished_at: datetime | None


class ApprovalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str | None
    run_id: str
    tool_name: str
    tool_use_id: str
    tool_input: dict
    rationale: str
    status: str
    resolved_by: str | None
    rejection_reason: str
    created_at: datetime
    resolved_at: datetime | None


class ApprovalResolve(BaseModel):
    decision: str  # "approve" or "reject"
    resolved_by: str = "operator"
    reason: str = ""


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    kind: str
    data: dict
    created_at: datetime


class TemplateOut(BaseModel):
    key: str
    name: str
    description: str
    default_tools: list[str]


class TriggerRequest(BaseModel):
    """Payload used by the manual /trigger endpoint."""

    payload: dict = Field(default_factory=dict)
    dry_run: bool | None = None
