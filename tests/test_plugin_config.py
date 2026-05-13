"""Plugin config schema, resolution, doctor."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from pilothouse.db import session
from pilothouse.events import reset_bus
from pilothouse.models import PluginConfig, PluginState
from pilothouse.plugins.manager import PluginManager, reset_manager


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    reset_manager()
    reset_bus()
    monkeypatch.delenv("CONFIGURABLE_WEBHOOK_URL", raising=False)
    yield
    reset_manager()


FIXTURES_DIR = Path(__file__).parent / "fixtures"


# --- discovery + misconfig flagging --------------------------------------


async def test_required_field_missing_marks_plugin_misconfigured(monkeypatch) -> None:
    monkeypatch.setenv("PILOTHOUSE_PLUGIN_DIR", str(FIXTURES_DIR))
    mgr = PluginManager()
    await mgr.load_all()

    rows = {r["name"]: r for r in mgr.list_plugins()}
    assert "configurable" in rows
    assert rows["configurable"]["enabled"] is True  # enabled by default
    assert "missing required config" in rows["configurable"]["misconfig_reason"]
    assert "webhook_url" in rows["configurable"]["misconfig_reason"]

    # Doctor surfaces it.
    bad = mgr.doctor()
    assert any(b["name"] == "configurable" for b in bad)


async def test_env_fallback_satisfies_required_field(monkeypatch) -> None:
    monkeypatch.setenv("PILOTHOUSE_PLUGIN_DIR", str(FIXTURES_DIR))
    monkeypatch.setenv("CONFIGURABLE_WEBHOOK_URL", "https://example.invalid/x")
    mgr = PluginManager()
    await mgr.load_all()

    rows = {r["name"]: r for r in mgr.list_plugins()}
    assert rows["configurable"]["misconfig_reason"] == ""

    cfg = await mgr.get_config("configurable", mask_secrets=False)
    assert cfg["webhook_url"]["value"] == "https://example.invalid/x"
    assert cfg["webhook_url"]["source"] == "env:CONFIGURABLE_WEBHOOK_URL"
    # Optional field falls back to its default.
    assert cfg["prefix"]["value"] == "[bot]"
    assert cfg["prefix"]["source"] == "default"


async def test_secret_value_masked_by_default(monkeypatch) -> None:
    monkeypatch.setenv("PILOTHOUSE_PLUGIN_DIR", str(FIXTURES_DIR))
    monkeypatch.setenv("CONFIGURABLE_WEBHOOK_URL", "supersecretvalue1234")
    mgr = PluginManager()
    await mgr.load_all()

    masked = await mgr.get_config("configurable")
    assert masked["webhook_url"]["value"].startswith("***")
    assert "supersecret" not in masked["webhook_url"]["value"]
    revealed = await mgr.get_config("configurable", mask_secrets=False)
    assert revealed["webhook_url"]["value"] == "supersecretvalue1234"


# --- set / unset cycle ---------------------------------------------------


async def test_set_config_persists_and_activates(monkeypatch) -> None:
    monkeypatch.setenv("PILOTHOUSE_PLUGIN_DIR", str(FIXTURES_DIR))
    mgr = PluginManager()
    await mgr.load_all()

    # Initially misconfigured (no env, no operator value).
    bad_before = {b["name"] for b in mgr.doctor()}
    assert "configurable" in bad_before

    await mgr.set_config("configurable", "webhook_url", "https://operator-set/x")

    # Persisted in DB.
    async with session() as s:
        row = (
            await s.execute(
                select(PluginConfig).where(
                    PluginConfig.plugin_name == "configurable",
                    PluginConfig.key == "webhook_url",
                )
            )
        ).scalar_one()
    assert row.value == "https://operator-set/x"

    # No longer misconfigured.
    bad_after = {b["name"] for b in mgr.doctor()}
    assert "configurable" not in bad_after

    # PluginState row's misconfig_reason is cleared.
    async with session() as s:
        state = (
            await s.execute(
                select(PluginState).where(PluginState.name == "configurable")
            )
        ).scalar_one()
    assert state.misconfig_reason == ""


async def test_unset_config_re_marks_misconfigured(monkeypatch) -> None:
    monkeypatch.setenv("PILOTHOUSE_PLUGIN_DIR", str(FIXTURES_DIR))
    mgr = PluginManager()
    await mgr.load_all()
    await mgr.set_config("configurable", "webhook_url", "https://set/x")
    assert "configurable" not in {b["name"] for b in mgr.doctor()}

    await mgr.unset_config("configurable", "webhook_url")
    assert "configurable" in {b["name"] for b in mgr.doctor()}


async def test_set_unknown_key_rejected(monkeypatch) -> None:
    monkeypatch.setenv("PILOTHOUSE_PLUGIN_DIR", str(FIXTURES_DIR))
    mgr = PluginManager()
    await mgr.load_all()
    with pytest.raises(KeyError):
        await mgr.set_config("configurable", "not_a_real_field", "x")


# --- configure() called with resolved values -----------------------------


async def test_configure_receives_merged_values(monkeypatch) -> None:
    monkeypatch.setenv("PILOTHOUSE_PLUGIN_DIR", str(FIXTURES_DIR))
    monkeypatch.setenv("CONFIGURABLE_WEBHOOK_URL", "from-env")
    mgr = PluginManager()
    await mgr.load_all()

    # Operator override should take precedence over env.
    await mgr.set_config("configurable", "webhook_url", "from-operator")

    # Find the live plugin instance and inspect what configure() saw.
    live = mgr._plugins["configurable"]
    assert live.plugin.received_config is not None
    assert live.plugin.received_config["webhook_url"] == "from-operator"
    assert live.plugin.received_config["prefix"] == "[bot]"


# --- HTTP surface --------------------------------------------------------


async def test_http_endpoints_for_plugin_config(monkeypatch) -> None:
    monkeypatch.setenv("PILOTHOUSE_PLUGIN_DIR", str(FIXTURES_DIR))
    from httpx import ASGITransport, AsyncClient

    from pilothouse.api.server import build_app

    # ASGITransport doesn't trigger the FastAPI lifespan, so the manager
    # would be empty without an explicit load_all. Production runs go
    # through the lifespan; tests need this nudge.
    mgr = PluginManager()
    await mgr.load_all()
    # Replace the global manager so HTTP handlers see the same state.
    from pilothouse.plugins import manager as mgr_mod

    mgr_mod._manager = mgr

    app = build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        # Listing reflects misconfig flag for new plugins.
        listing = (await c.get("/plugins")).json()
        cfg_row = next(p for p in listing if p["name"] == "configurable")
        assert cfg_row["misconfig_reason"] != ""
        assert any(f["name"] == "webhook_url" for f in cfg_row["config_schema"])

        # /plugins/doctor reports it.
        doctor = (await c.get("/plugins/doctor")).json()
        assert any(d["name"] == "configurable" for d in doctor["misconfigured"])

        # Set via HTTP.
        r = await c.post(
            "/plugins/configurable/config",
            json={"key": "webhook_url", "value": "https://set-via-http/x"},
        )
        assert r.status_code == 200, r.text

        # Now doctor is clean.
        doctor2 = (await c.get("/plugins/doctor")).json()
        assert not any(d["name"] == "configurable" for d in doctor2["misconfigured"])

        # GET config (masked).
        cfg = (await c.get("/plugins/configurable/config")).json()
        assert cfg["webhook_url"]["value"].startswith("***")
        assert cfg["webhook_url"]["source"] == "operator"
