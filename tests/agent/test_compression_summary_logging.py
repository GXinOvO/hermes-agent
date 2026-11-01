"""Rejecting incomplete checkpoints must not claim that a clipped prefix was accepted."""

from unittest.mock import patch

from agent.auxiliary_client import extract_content_or_reasoning
from agent.context_compressor import ContextCompressor


def test_rejected_reasoning_does_not_log_a_clipped_summary(caplog):
    response = {"choices": [{"message": {"content": "", "reasoning_content": "r" * 8_001}}]}
    compressor = ContextCompressor(model="test/model", config_context_length=100_000, quiet_mode=True)
    with patch("agent.context_compressor.call_llm", return_value=response):
        assert compressor._generate_summary([
            {"role": "user", "content": "preserve this task"}, {"role": "assistant", "content": "working"},
        ]) is None
    assert "truncating to 8000" not in caplog.text


def test_shared_extractor_still_logs_and_returns_a_bounded_prefix(caplog):
    response = {"content": "", "reasoning_content": "r" * 8_001}
    assert extract_content_or_reasoning(response, max_reasoning_chars=8_000) == "r" * 8_000
    assert "truncating to 8000" in caplog.text
