"""Stored bare-custom routes recover their account group before building a Desktop agent."""

import json
from types import SimpleNamespace

import pytest

from hermes_cli.config import atomic_config_write, get_config_path
from hermes_state import SessionDB
from tui_gateway import server


RELAY = "https://relay.example/v1"


@pytest.mark.parametrize("nested", [False, True], ids=["desktop-row", "gateway-row"])
def test_reopened_legacy_session_resolves_the_model_group_without_storing_secrets(tmp_path, nested):
    config = {
        "providers": {
            "writer": {"base_url": RELAY, "api_key": "writer-fixture-key", "model": "writer-model"},
            "reasoning": {"base_url": RELAY, "api_key": "reasoning-fixture-key", "model": "reasoning-model"},
        },
    }
    atomic_config_write(get_config_path(), config)
    route = {"provider": "custom", "base_url": RELAY, "api_mode": "chat_completions"}
    db_path = tmp_path / "sessions.db"
    with SessionDB(db_path) as db:
        db.create_session(
            "legacy", source="desktop", model="reasoning-model",
            model_config={"gateway_runtime": route} if nested else route,
        )
    with SessionDB(db_path) as db:
        overrides = server._stored_session_runtime_overrides(db.get_session("legacy"))
        assert overrides["provider_override"] == "custom:reasoning"
        model, runtime = server._resolve_agent_model_runtime(
            overrides["model_override"], overrides["provider_override"]
        )
        assert runtime["api_key"] == "reasoning-fixture-key"
        agent = SimpleNamespace(**{**runtime, "model": model, "reasoning_config": None, "service_tier": None})
        saved = server._runtime_model_config(agent)
        assert saved["provider"] == "custom:reasoning"
        assert "api_key" not in saved
        assert "fixture-key" not in json.dumps(saved)
        db.update_session_meta("legacy", json.dumps(saved), model)
    with SessionDB(db_path) as db:
        restored = server._stored_session_runtime_overrides(db.get_session("legacy"))
        assert restored["provider_override"] == "custom:reasoning"
        _, runtime = server._resolve_agent_model_runtime(
            restored["model_override"], restored["provider_override"]
        )
        assert runtime["api_key"] == "reasoning-fixture-key"
