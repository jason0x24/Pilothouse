"""YAML manifest plan / apply tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from pilothouse.db import session
from pilothouse.declarative import (
    Manifest,
    apply_plan,
    compute_plan,
    load_manifest,
)
from pilothouse.models import Agent


MANIFEST_YAML = """
version: 1
defaults:
  dry_run: true
prune: false
agents:
  - name: triage-a
    template: datadog_alert_triage
    description: alpha
    params:
      service: checkout
    enabled: true
  - name: scanner-a
    template: pr_security_scanner
    params:
      repo: acme/api
      auto_comment: false
    schedule_cron: "*/30 * * * *"
"""


async def test_plan_for_empty_db_is_all_creates(tmp_path: Path) -> None:
    p = tmp_path / "agents.yaml"
    p.write_text(MANIFEST_YAML)
    manifest = load_manifest(p)

    plan = await compute_plan(manifest)
    actions = [i.action for i in plan.items]
    assert sorted(actions) == ["create", "create"]
    assert plan.summary()["create"] == 2


async def test_apply_creates_agents_and_subsequent_plan_is_noop(tmp_path: Path) -> None:
    p = tmp_path / "agents.yaml"
    p.write_text(MANIFEST_YAML)
    manifest = load_manifest(p)

    plan = await compute_plan(manifest)
    await apply_plan(manifest, plan)

    async with session() as s:
        rows = (await s.execute(select(Agent))).scalars().all()
    names = {a.name for a in rows}
    assert names == {"triage-a", "scanner-a"}

    plan2 = await compute_plan(manifest)
    assert plan2.summary() == {"create": 0, "update": 0, "delete": 0, "noop": 2}


async def test_update_diff_reflects_changed_fields(tmp_path: Path) -> None:
    p = tmp_path / "agents.yaml"
    p.write_text(MANIFEST_YAML)
    manifest = load_manifest(p)
    await apply_plan(manifest, await compute_plan(manifest))

    # Mutate the manifest in-memory: tweak description and enabled.
    manifest.agents[0].description = "alpha updated"
    manifest.agents[0].enabled = False

    plan = await compute_plan(manifest)
    update_item = next(i for i in plan.items if i.name == "triage-a")
    assert update_item.action == "update"
    assert "description" in update_item.diff
    assert update_item.diff["description"] == ("alpha", "alpha updated")
    assert update_item.diff["enabled"] == (True, False)


async def test_prune_drops_agents_not_in_manifest(tmp_path: Path) -> None:
    p = tmp_path / "agents.yaml"
    p.write_text(MANIFEST_YAML)
    manifest = load_manifest(p)
    await apply_plan(manifest, await compute_plan(manifest))

    # Now reduce the manifest to a single agent + prune.
    manifest = Manifest(
        version=1,
        defaults={"dry_run": True},
        prune=True,
        agents=[manifest.agents[0]],  # keep only triage-a
    )
    plan = await compute_plan(manifest)
    actions = sorted(i.action for i in plan.items if i.action != "noop")
    assert actions == ["delete"]
    await apply_plan(manifest, plan)
    async with session() as s:
        rows = (await s.execute(select(Agent))).scalars().all()
    assert {a.name for a in rows} == {"triage-a"}


async def test_unknown_template_rejected(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text(
        """
        version: 1
        agents:
          - name: x
            template: not_a_real_template
        """
    )
    manifest = load_manifest(p)
    with pytest.raises(ValueError):
        await compute_plan(manifest)
