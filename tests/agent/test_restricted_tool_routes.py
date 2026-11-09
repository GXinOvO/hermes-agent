from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from agent import agent_init
from run_agent import AIAgent


@pytest.mark.parametrize("route", [
    {"provider": "acp", "base_url": "acp://unit"},
    {"provider": "custom", "base_url": "acp://unit"},
    {"provider": "custom", "api_mode": "codex_app_server"},
    {"provider": "custom", "acp_command": "unit-never-started"},
])
@pytest.mark.parametrize("allowed", [[], ["read_file"]])
def test_restricted_constructor_refuses_external_route_before_client(route, allowed, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("model:\n  context_length: 131072\n")
    kwargs: dict[str, Any] = dict(api_key="unit-no-network", provider="custom", base_url="http://127.0.0.1:9/v1",
                  api_mode="chat_completions", model="unit", quiet_mode=True, skip_context_files=True,
                  skip_memory=True, skip_background_review=True, enabled_toolsets=["file"], allowed_tools=allowed)
    kwargs.update(route)
    with patch.object(agent_init, "_build_client", side_effect=AssertionError("client entry must remain unreachable")) as build:
        with pytest.raises(ValueError, match="allowed_tools.*route"):
            AIAgent(**kwargs)
    build.assert_not_called()


def test_external_fallback_is_refused_before_resolving_client():
    from agent.route_binding import bind_route_entry
    agent = SimpleNamespace(allowed_tools=[], provider="custom", api_mode="chat_completions", base_url="http://unit")
    with patch("agent.auxiliary_client.resolve_provider_client", return_value=(None, None)) as resolve:
        with pytest.raises(ValueError, match="allowed_tools.*route"):
            bind_route_entry(agent, {"provider": "acp", "model": "unit", "base_url": "acp://unit"}, "acp", "unit")
    resolve.assert_not_called()


def test_external_request_is_refused_before_sdk_or_middleware():
    from agent.turn_api_call import perform_api_call
    agent = SimpleNamespace(allowed_tools=[], provider="custom", api_mode="chat_completions", base_url="acp://unit",
                            session_id="unit", platform="cli", model="unit", _has_pending_redirect=lambda: False)
    with patch("hermes_cli.middleware.run_llm_execution_middleware") as middleware:
        with pytest.raises(ValueError, match="allowed_tools.*route"):
            perform_api_call(agent, api_kwargs={}, _original_api_kwargs={}, _llm_middleware_trace=[],
                             _moa_prepared_request=None, _retry=None, thinking_spinner=None, retry_count=0,
                             api_call_count=0, api_request_id="unit", effective_task_id="unit", turn_id="unit", interrupted=False)
    middleware.assert_not_called()


def test_codex_turn_refuses_before_opening_its_independent_tool_bridge():
    from agent.codex_runtime import run_codex_app_server_turn
    agent = SimpleNamespace(allowed_tools=[], provider="custom", api_mode="codex_app_server")
    with patch("agent.codex_runtime._ensure_codex_session") as ensure:
        with pytest.raises(ValueError, match="allowed_tools.*route"):
            run_codex_app_server_turn(agent, user_message="unit", original_user_message="unit",
                                      messages=[], effective_task_id="unit")
    ensure.assert_not_called()


def test_omitted_restriction_keeps_existing_external_route():
    from tools.delegate_tool_scope import validate_restricted_route
    validate_restricted_route(SimpleNamespace(provider="acp", base_url="acp://unit", api_mode="chat_completions"))


def test_native_http_constructor_keeps_exact_tool_surface(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("model:\n  context_length: 131072\n")
    agent = AIAgent(api_key="unit-no-network", provider="custom", base_url="http://127.0.0.1:9/v1",
                    api_mode="chat_completions", model="unit", quiet_mode=True, skip_context_files=True,
                    skip_memory=True, skip_background_review=True, enabled_toolsets=["file"], allowed_tools=["read_file"])
    try:
        assert agent.valid_tool_names == {"read_file"}
    finally:
        agent.close()
