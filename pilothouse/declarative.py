"""Declarative agent management — `pilothouse apply -f agents.yaml`.

The DevOps audience expects to define their automations in version-
controlled files and apply them like Terraform / kubectl. This module
implements that workflow:

  * Parse a YAML/JSON file into a `Manifest` (validated).
  * Diff against the current DB → `Plan` with creates / updates / deletes.
  * Apply the plan in one transaction (or report what *would* happen
    with `--dry-run`).

The manifest schema is intentionally tiny — it mirrors `AgentCreate`
plus an optional top-level `defaults` block, plus a `prune: bool` flag
that, when true, deletes any agent whose name is not in the file.

Example agents.yaml:

  version: 1
  defaults:
    dry_run: true
  prune: false
  agents:
    - name: triage-checkout
      template: datadog_alert_triage
      description: Investigate checkout latency alerts
      params:
        service: checkout
        slack_channel: "#sre-checkout"
      schedule_cron: null
      enabled: true
    - name: nightly-flaky-scan
      template: flaky_test_hunter
      params:
        repo: acme/api
        tracking_issue: 42
      schedule_cron: "0 5 * * *"

We deliberately don't roll our own type coercion — Pydantic does it and
gives nice errors at the boundary, which is the only place a manifest
ever crosses into the system.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from .db import session
from .models import Agent
from .templates.base import registry as tpl_registry


class ManifestAgent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    template: str
    description: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    schedule_cron: str | None = None
    enabled: bool = True
    dry_run: bool | None = None  # falls back to defaults.dry_run, then True


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    defaults: dict[str, Any] = Field(default_factory=dict)
    prune: bool = False
    agents: list[ManifestAgent] = Field(default_factory=list)

    def resolved(self) -> list[ManifestAgent]:
        """Apply defaults so callers don't have to."""
        dr = self.defaults.get("dry_run", True)
        return [
            a.model_copy(update={"dry_run": dr if a.dry_run is None else a.dry_run})
            for a in self.agents
        ]


@dataclass
class PlanItem:
    name: str
    action: str  # "create" | "update" | "delete" | "noop"
    diff: dict = field(default_factory=dict)  # before → after


@dataclass
class Plan:
    items: list[PlanItem]

    @property
    def changed(self) -> list[PlanItem]:
        return [i for i in self.items if i.action != "noop"]

    def summary(self) -> dict:
        c = {"create": 0, "update": 0, "delete": 0, "noop": 0}
        for i in self.items:
            c[i.action] += 1
        return c


def load_manifest(path: str | Path) -> Manifest:
    p = Path(path)
    text = p.read_text()
    if p.suffix in {".yaml", ".yml"}:
        raw = yaml.safe_load(text) or {}
    elif p.suffix == ".json":
        raw = json.loads(text)
    else:
        # Try YAML first; fall back to JSON.
        try:
            raw = yaml.safe_load(text) or {}
        except yaml.YAMLError:
            raw = json.loads(text)
    if not isinstance(raw, dict):
        raise ValueError(f"manifest must be a mapping at the top level, got {type(raw).__name__}")
    try:
        return Manifest(**raw)
    except ValidationError as exc:
        raise ValueError(f"invalid manifest: {exc}") from exc


def validate_templates(manifest: Manifest) -> None:
    """Fail fast if any manifest agent references an unknown template."""
    known = set(tpl_registry.templates.keys())
    unknown = sorted({a.template for a in manifest.agents} - known)
    if unknown:
        raise ValueError(
            f"unknown templates referenced: {', '.join(unknown)}. "
            f"Available: {', '.join(sorted(known))}"
        )


async def compute_plan(manifest: Manifest, *, tenant_id: str | None = None) -> Plan:
    """Compare manifest to a tenant's current agents; build a Plan with no writes.

    `tenant_id=None` means "default tenant" — convenient for the CLI which
    is single-tenant. The HTTP layer always passes an explicit tenant.
    """
    from .tenants import ensure_default_tenant

    validate_templates(manifest)
    desired = {a.name: a for a in manifest.resolved()}
    tid = tenant_id or await ensure_default_tenant()

    async with session() as s:
        rows = (
            await s.execute(select(Agent).where(Agent.tenant_id == tid))
        ).scalars().all()
    current = {a.name: a for a in rows}

    items: list[PlanItem] = []

    # Creates + updates
    for name, want in desired.items():
        if name not in current:
            items.append(PlanItem(name=name, action="create", diff={"after": want.model_dump()}))
            continue
        have = current[name]
        before = {
            "template": have.template,
            "description": have.description,
            "params": have.params or {},
            "schedule_cron": have.schedule_cron,
            "enabled": have.enabled,
            "dry_run": have.dry_run,
        }
        after = {
            "template": want.template,
            "description": want.description,
            "params": want.params,
            "schedule_cron": want.schedule_cron,
            "enabled": want.enabled,
            "dry_run": bool(want.dry_run),
        }
        if before == after:
            items.append(PlanItem(name=name, action="noop"))
        else:
            changed = {k: (before[k], after[k]) for k in after if before[k] != after[k]}
            items.append(PlanItem(name=name, action="update", diff=changed))

    # Deletes (only when prune=true)
    if manifest.prune:
        for name in current:
            if name not in desired:
                items.append(PlanItem(name=name, action="delete", diff={"before": current[name].name}))

    return Plan(items=items)


async def apply_plan(
    manifest: Manifest, plan: Plan, *, tenant_id: str | None = None
) -> Plan:
    """Persist a Plan into a specific tenant. Returns the same Plan."""
    from .tenants import ensure_default_tenant

    desired = {a.name: a for a in manifest.resolved()}
    tid = tenant_id or await ensure_default_tenant()
    sched = None
    try:
        from .scheduler import get_scheduler

        sched = get_scheduler()
    except Exception:
        sched = None

    async with session() as s:
        rows = {
            a.name: a
            for a in (
                await s.execute(select(Agent).where(Agent.tenant_id == tid))
            ).scalars().all()
        }
        for item in plan.items:
            if item.action == "create":
                want = desired[item.name]
                a = Agent(
                    tenant_id=tid,
                    name=want.name,
                    template=want.template,
                    description=want.description,
                    params=want.params,
                    schedule_cron=want.schedule_cron,
                    enabled=want.enabled,
                    dry_run=bool(want.dry_run),
                )
                s.add(a)
                await s.flush()
                if sched is not None and a.schedule_cron and a.enabled:
                    await sched.add_or_update(a.id, a.schedule_cron)
            elif item.action == "update":
                want = desired[item.name]
                a = rows[item.name]
                a.template = want.template
                a.description = want.description
                a.params = want.params
                a.schedule_cron = want.schedule_cron
                a.enabled = want.enabled
                a.dry_run = bool(want.dry_run)
                if sched is not None:
                    if a.enabled and a.schedule_cron:
                        await sched.add_or_update(a.id, a.schedule_cron)
                    else:
                        await sched.remove(a.id)
            elif item.action == "delete":
                a = rows[item.name]
                if sched is not None:
                    await sched.remove(a.id)
                await s.delete(a)
            # noop → nothing
    return plan


def render_plan(plan: Plan) -> str:
    """Human-readable plan, used by the CLI."""
    lines: list[str] = []
    sym = {"create": "+", "update": "~", "delete": "-", "noop": " "}
    for i in plan.items:
        if i.action == "noop":
            continue
        lines.append(f"{sym[i.action]} {i.name}    [{i.action}]")
        if i.action == "update":
            for k, (before, after) in i.diff.items():
                lines.append(f"    {k}: {before!r} → {after!r}")
        elif i.action == "create":
            after = i.diff.get("after", {})
            lines.append(f"    template={after.get('template')}  cron={after.get('schedule_cron') or '-'}  dry_run={after.get('dry_run')}")
    if not lines:
        lines.append("  (no changes)")
    summary = plan.summary()
    lines.append("")
    lines.append(
        f"  Plan: {summary['create']} to add, {summary['update']} to change, "
        f"{summary['delete']} to delete, {summary['noop']} unchanged."
    )
    return "\n".join(lines)
