"""Plugin manager / discovery / lifecycle tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import select

from pilothouse.connectors.base import registry as conn_registry
from pilothouse.db import session
from pilothouse.events import RunEvent, get_bus, reset_bus
from pilothouse.models import Agent, PluginState
from pilothouse.orchestration.executor import execute_agent
from pilothouse.plugins.manager import PluginManager, reset_manager
from pilothouse.templates.base import registry as tpl_registry


@pytest.fixture(autouse=True)
def _reset_plugin_manager():
    reset_manager()
    reset_bus()
    yield
    reset_manager()


FIXTURES_DIR = Path(__file__).parent / "fixtures"


# --- builtin discovery -------------------------------------------------


async def test_load_all_registers_builtin_templates_and_connectors() -> None:
    mgr = PluginManager()
    await mgr.load_all()
    rows = mgr.list_plugins()
    names = {r["name"] for r in rows}
    # Every built-in template + connector becomes a plugin row.
    assert "builtin.template.datadog_alert_triage" in names
    assert "builtin.template.bug_auto_fixer" in names
    assert "builtin.template.pr_code_reviewer" in names
    assert "builtin.github" in names
    assert "builtin.linear" in names

    # Registries are populated (regardless of which path put them there).
    assert "datadog_alert_triage" in tpl_registry.templates
    assert "github" in conn_registry.connectors


async def test_disable_template_plugin_removes_from_registry() -> None:
    mgr = PluginManager()
    await mgr.load_all()
    assert "k8s_pod_investigator" in tpl_registry.templates

    await mgr.disable("builtin.template.k8s_pod_investigator")
    assert "k8s_pod_investigator" not in tpl_registry.templates

    # State is persisted.
    async with session() as s:
        row = (
            await s.execute(
                select(PluginState).where(
                    PluginState.name == "builtin.template.k8s_pod_investigator"
                )
            )
        ).scalar_one()
    assert row.enabled is False

    # Re-enabling restores.
    await mgr.enable("builtin.template.k8s_pod_investigator")
    assert "k8s_pod_investigator" in tpl_registry.templates


async def test_disable_persists_across_manager_restarts() -> None:
    mgr = PluginManager()
    await mgr.load_all()
    await mgr.disable("builtin.template.flaky_test_hunter")

    # Simulate a process restart by reset + new manager.
    reset_manager()
    mgr2 = PluginManager()
    await mgr2.load_all()
    rows = {r["name"]: r for r in mgr2.list_plugins()}
    assert rows["builtin.template.flaky_test_hunter"]["enabled"] is False
    # Registry should reflect the persisted state — template not present.
    assert "flaky_test_hunter" not in tpl_registry.templates


# --- directory discovery ----------------------------------------------


async def test_directory_discovery_picks_up_local_plugin(monkeypatch) -> None:
    monkeypatch.setenv("PILOTHOUSE_PLUGIN_DIR", str(FIXTURES_DIR))
    # Drop in our sample plugin file path. The fixture file already exists.
    mgr = PluginManager()
    await mgr.load_all()

    rows = {r["name"]: r for r in mgr.list_plugins()}
    assert "sample" in rows
    assert rows["sample"]["source"].startswith("directory:")
    assert "connector" in rows["sample"]["kinds"]
    assert "notifier" in rows["sample"]["kinds"]

    # The echo connector + its tool should be in the registry.
    assert "echo" in conn_registry.connectors
    assert "echo_say" in {t.name for t in conn_registry.all_tools().values()}


async def test_notifier_plugin_receives_approval_requested_events(
    monkeypatch,
) -> None:
    monkeypatch.setenv("PILOTHOUSE_PLUGIN_DIR", str(FIXTURES_DIR))
    mgr = PluginManager()
    await mgr.load_all()

    from tests.fixtures.sample_plugin_sink import EVENTS

    EVENTS.clear()

    # Trigger a non-dry-run scanner so the runtime emits approval_requested.
    async with session() as s:
        a = Agent(
            name="plugin-notify-test",
            template="pr_security_scanner",
            params={"repo": "acme/api", "auto_comment": True},
            dry_run=False,
        )
        s.add(a)
        await s.flush()
        aid = a.id
    await execute_agent(
        agent_id=aid,
        trigger="manual",
        trigger_payload={"pull_request": {"number": 1}, "repository": {"full_name": "acme/api"}},
    )

    # Let the notifier task drain.
    for _ in range(50):
        if EVENTS:
            break
        await asyncio.sleep(0.02)
    assert EVENTS, "expected the notifier plugin to be invoked"


# --- enable/disable lifecycle for notifier plugins --------------------


async def test_disabling_notifier_plugin_stops_dispatch(monkeypatch) -> None:
    monkeypatch.setenv("PILOTHOUSE_PLUGIN_DIR", str(FIXTURES_DIR))
    mgr = PluginManager()
    await mgr.load_all()

    from tests.fixtures.sample_plugin_sink import EVENTS

    EVENTS.clear()
    await mgr.disable("sample")

    # Now fire an event manually; nothing should be captured.
    get_bus().publish(RunEvent(run_id="x", kind="approval_requested", data={"approval_id": "a"}))
    await asyncio.sleep(0.05)
    assert EVENTS == []


# --- duplicate-name protection ----------------------------------------


async def test_duplicate_names_are_skipped() -> None:
    """Re-running load_all twice doesn't double-register the built-ins
    (they all share names like `builtin.github` and the manager dedupes
    by name)."""
    mgr = PluginManager()
    await mgr.load_all()
    n1 = len(mgr.list_plugins())
    await mgr.load_all()
    n2 = len(mgr.list_plugins())
    assert n1 == n2
