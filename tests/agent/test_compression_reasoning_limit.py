"""A locally cut reasoning fallback must never become a durable checkpoint.

Providers can return a completed response with the summary in reasoning fields.
A bounded prefix of that text is still incomplete even when finish_reason=stop.
"""

from copy import deepcopy
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from agent.context_compressor import ContextCompressor
from agent.conversation_compression import compress_context
from hermes_state import SessionDB


REQUIREMENT = "Keep export atomic; do not publish before validation."
UNFINISHED = "Unfinished: verify rows against evidence/export-receipt.json."
LATEST = "Continue validation and preserve the original export constraints."


def _response(reasoning, field):
    message: dict[str, Any] = {"content": ""}
    message[field] = [{"text": reasoning}] if field == "reasoning_details" else reasoning
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")]
    )


def _messages():
    messages = [{"role": "user", "content": REQUIREMENT}]
    for index in range(20):
        messages.extend([
            {"role": "assistant", "content": f"investigation {index}: " + "record " * 400},
            {"role": "user", "content": f"Preserve receipt {index} and continue."},
        ])
    messages.extend([
        {"role": "assistant", "content": UNFINISHED},
        {"role": "user", "content": LATEST},
    ])
    return messages


def _agent(db) -> Any:
    from run_agent import AIAgent

    agent = AIAgent(
        model="test/model", provider="openrouter", api_key="test-key",
        base_url="https://openrouter.ai/api/v1", quiet_mode=True,
        session_db=db, session_id="reasoning-limit", skip_context_files=True,
        skip_memory=True,
    )
    agent._compression_feasibility_checked = True
    agent.compression_in_place = True
    agent._cached_system_prompt = "sys"
    agent.context_compressor = ContextCompressor(
        model="test/model", provider="openrouter", api_key="test-key",
        config_context_length=100_000, protect_first_n=1, protect_last_n=1,
        quiet_mode=True,
    )
    agent.context_compressor.tail_token_budget = 50
    agent.context_compressor.bind_session_state(
        session_db=db, session_id=agent.session_id,
    )
    return agent


@pytest.mark.parametrize("field", ["reasoning", "reasoning_content", "reasoning_details"])
def test_over_limit_reasoning_preserves_history_and_blocks_reentry(tmp_path, field):
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    db.create_session("reasoning-limit", source="cli")
    agent = _agent(db)
    messages = _messages()
    agent._flush_messages_to_session_db(messages, [])
    before = deepcopy(messages)
    rows_before = db.get_messages(agent.session_id)
    compressor = agent.context_compressor
    compressor._previous_summary = "Known complete checkpoint."
    oversized = "## Goal\nKeep the export safe.\n" + "working notes " * 700 + "\n" + UNFINISHED

    with patch("agent.context_compressor.call_llm", return_value=_response(oversized, field)) as call:
        returned, _ = compress_context(agent, messages, "sys", approx_tokens=90_000)
        assert returned == before
        assert compressor._previous_summary == "Known complete checkpoint."
        assert compressor._last_compress_aborted is True
        assert compressor._last_summary_truncated_failure is True
        assert compressor._last_summary_dropped_count == 0
        assert db.get_messages(agent.session_id) == rows_before
        assert db.get_compression_failure_cooldown(agent.session_id)
        assert "length" not in compressor._last_summary_error
        again, _ = compress_context(agent, returned, "sys", approx_tokens=90_000)
        assert again == before
        assert call.call_count == 1

    db.close()
    reopened = SessionDB(db_path=path)
    try:
        assert reopened.get_messages("reasoning-limit") == rows_before
        restarted = _agent(reopened)
        with patch("agent.context_compressor.call_llm") as call:
            returned, _ = compress_context(restarted, deepcopy(before), "sys", approx_tokens=90_000)
            assert returned == before
            call.assert_not_called()
    finally:
        reopened.close()


def test_over_limit_aux_reasoning_retries_once_with_complete_main_summary():
    compressor = ContextCompressor(
        model="main-model", provider="openrouter", api_key="test-key",
        summary_model_override="aux-model", config_context_length=100_000,
        quiet_mode=True,
    )
    oversized = "notes " * 1500 + "\n" + UNFINISHED
    complete = "## Goal\n" + REQUIREMENT + "\n## Blocked\n" + UNFINISHED
    responses = [
        _response(oversized, "reasoning_content"),
        SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=complete), finish_reason="stop",
        )]),
    ]
    with patch("agent.context_compressor.call_llm", side_effect=responses) as call:
        summary = compressor._generate_summary(_messages())

    assert call.call_count == 2
    assert call.call_args_list[1].kwargs["model"] == "main-model"
    assert summary is not None
    assert REQUIREMENT in summary and UNFINISHED in summary
    assert oversized[:8000] not in summary
    assert compressor._last_summary_truncated_failure is False
