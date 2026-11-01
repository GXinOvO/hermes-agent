"""Same-named summary models on distinct backends must still retry the main route."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.context_compressor import ContextCompressor


def _messages():
    return [{"role": "user", "content": "preserve this task"}, {"role": "assistant", "content": "working"}]


@pytest.mark.parametrize("aux_provider,aux_url,should_retry", [
    ("openrouter", "https://aux.invalid/v1", True),
    ("custom", "https://aux.invalid/v1", True),
    ("custom", "https://main.invalid/v1/", False),
    ("custom", "HTTPS://MAIN.INVALID:443/v1", False),
    ("custom", "https://main.invalid/other", True),
    ("custom:alias", "https://main.invalid/v1", False),
])
def test_same_model_failure_compares_actual_backend(aux_provider, aux_url, should_retry):
    """Use real call_llm resolution/route recording; replace client discovery and transport."""
    config = {"auxiliary": {"compression": {
        "provider": aux_provider, "model": "same-model", "base_url": aux_url,
        "api_key": "aux-fixture", "api_mode": "chat_completions",
    }}}
    calls = []

    def client_for(provider, model=None, **kwargs):
        return SimpleNamespace(base_url=kwargs.get("base_url") or aux_url), model

    def transport(client, request, task, **kwargs):
        calls.append((str(client.base_url), dict(request)))
        complete = len(calls) == 2
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="complete main summary" if complete else "partial"),
            finish_reason="stop" if complete else "length",
        )])

    compressor = ContextCompressor(
        model="same-model", provider="custom", base_url="https://main.invalid/v1",
        api_key="main-fixture", api_mode="chat_completions", config_context_length=100_000,
        quiet_mode=True,
    )
    with (
        patch("hermes_cli.config.load_config_readonly", return_value=config),
        patch("hermes_cli.config.load_config", return_value=config),
        patch("agent.auxiliary_client._get_cached_client", side_effect=client_for),
        patch("agent.auxiliary_client._create_with_progress", side_effect=transport),
    ):
        result = compressor._generate_summary(_messages())
    assert len(calls) == (2 if should_retry else 1)
    if should_retry:
        assert result is not None and "complete main summary" in result
        assert calls[1][0] == "https://main.invalid/v1"
        assert compressor._last_aux_model_failure_model == "same-model"
    else:
        assert result is None
        assert compressor._last_summary_truncated_failure is True


def test_unresolved_call_does_not_reuse_a_prior_backend():
    compressor = ContextCompressor(
        model="same-model", provider="custom", config_context_length=100_000, quiet_mode=True,
    )

    def first_call(**kwargs):
        kwargs["route_info"].update(provider="openrouter", model="same-model")
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="complete summary"), finish_reason="stop",
        )])

    with patch("agent.context_compressor.call_llm", side_effect=first_call):
        assert compressor._generate_summary(_messages()) is not None
    with patch("agent.context_compressor.call_llm", side_effect=RuntimeError("transport unavailable")) as call:
        assert compressor._generate_summary(_messages()) is None
    assert call.call_count == 1


def test_main_retry_is_bounded_and_preserves_durable_history(tmp_path):
    from copy import deepcopy
    from hermes_state import SessionDB
    from agent.conversation_compression import compress_context
    from tests.agent.test_compression_reasoning_limit import _agent, _messages as history_messages

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("reasoning-limit", source="cli")
    agent = _agent(db)
    compressor = agent.context_compressor
    compressor.base_url = "https://openrouter.ai/api/v1"
    messages = history_messages()
    agent._flush_messages_to_session_db(messages, [])
    before, rows = deepcopy(messages), db.get_messages(agent.session_id)

    def truncated_call(**kwargs):
        kwargs["route_info"].update(
            provider="custom", model="test/model", base_url="https://aux.invalid/v1",
        )
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="partial"), finish_reason="length",
        )])

    try:
        with patch("agent.context_compressor.call_llm", side_effect=truncated_call) as call:
            returned, _ = compress_context(agent, messages, "sys", approx_tokens=90_000)
            assert call.call_count == 2
            retry = call.call_args_list[1].kwargs
            assert retry["provider"] == "openrouter"
            assert retry["base_url"] == "https://openrouter.ai/api/v1"
            assert returned == before
            assert db.get_messages(agent.session_id) == rows
            assert compressor._last_compress_aborted is True
    finally:
        db.close()


def test_internal_chain_does_not_repeat_an_already_failed_main_route():
    config = {"auxiliary": {"compression": {
        "provider": "custom", "model": "same-model", "base_url": "https://aux.invalid/v1",
        "api_key": "aux-fixture", "fallback_chain": [{
            "provider": "custom", "model": "same-model", "base_url": "https://main.invalid/v1",
            "api_key": "main-fixture",
        }],
    }}}
    calls = []

    def client_for(provider, model=None, **kwargs):
        return SimpleNamespace(base_url=kwargs.get("base_url") or "https://aux.invalid/v1"), model

    def transport(client, request, task, **kwargs):
        calls.append(str(client.base_url))
        raise ConnectionError("connection refused")

    compressor = ContextCompressor(
        model="same-model", provider="custom", base_url="https://main.invalid/v1",
        api_key="main-fixture", config_context_length=100_000, quiet_mode=True,
    )
    with (
        patch("hermes_cli.config.load_config_readonly", return_value=config),
        patch("hermes_cli.config.load_config", return_value=config),
        patch("agent.auxiliary_client._get_cached_client", side_effect=client_for),
        patch("agent.auxiliary_client._resolve_fallback_entry", return_value=(
            SimpleNamespace(base_url="https://main.invalid/v1"), "same-model")),
        patch("agent.auxiliary_client._candidate_context_window", return_value=100_000),
        patch("agent.auxiliary_client._transient_retry_count", return_value=0),
        patch("agent.auxiliary_client._create_with_progress", side_effect=transport),
    ):
        assert compressor._generate_summary(_messages()) is None
    assert calls == ["https://aux.invalid/v1", "https://main.invalid/v1"]
