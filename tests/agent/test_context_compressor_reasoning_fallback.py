"""Reasoning-model summaries must not trip the empty-content failure path.

Local / thinking backends (DeepSeek, Qwen, Kimi) often return content=""
with the usable text in reasoning / reasoning_content. _generate_summary
accepts a complete fallback, but never checkpoints a locally clipped prefix
of an oversized CoT dump. Empty outputs still fail closed (#11978).
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.context_compressor import ContextCompressor, SUMMARY_PREFIX


def _compressor(**overrides):
    kwargs = dict(model="test/model", quiet_mode=True, tail_mode="legacy")
    kwargs.update(overrides)
    with patch("agent.context_compressor.get_model_context_length", return_value=100000):
        return ContextCompressor(**kwargs)


def _turns():
    return [
        {"role": "user", "content": "do something"},
        {"role": "assistant", "content": "ok"},
    ]


@pytest.mark.parametrize("visible,reasoning,expected", [
    ("", "kept reasoning summary", "kept reasoning summary"),
    ("", "r" * 8_000, "r" * 8_000),
    ("complete visible summary " * 500, "r" * 20_000, "complete visible summary " * 500),
])
def test_complete_summary_is_preserved(visible, reasoning, expected):
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(
            content=visible,
            reasoning_content=reasoning,
        ))]
    )
    with patch("agent.context_compressor.call_llm", return_value=response):
        out = _compressor()._generate_summary(_turns())
    assert out is not None
    assert expected.strip() in out
    assert out.startswith(SUMMARY_PREFIX)


def test_whitespace_content_falls_back_for_dict_and_object():
    class _Msg:
        content = " "
        reasoning_content = "object reasoning"

    for message, needle in (
        ({"content": " ", "reasoning_content": "dict reasoning"}, "dict reasoning"),
        (_Msg(), "object reasoning"),
    ):
        response = {"choices": [{"message": message}]}
        with patch("agent.context_compressor.call_llm", return_value=response):
            out = _compressor()._generate_summary(_turns())
        assert out is not None
        assert needle in out


@pytest.mark.parametrize("size", [8_001, 20_000])
def test_oversized_reasoning_fallback_is_rejected(size):
    reasoning = "t" * size
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(
            content="",
            reasoning_content=reasoning,
        ))]
    )
    compressor = _compressor()
    compressor._previous_summary = "Last complete summary."
    with patch("agent.context_compressor.call_llm", return_value=response):
        out = compressor._generate_summary(_turns())
    assert out is None
    assert compressor._last_summary_truncated_failure is True
    assert compressor._previous_summary == "Last complete summary."


@pytest.mark.parametrize("response_shape", ["object", "dict"])
@pytest.mark.parametrize("reasoning_size", [100, 8_001])
def test_provider_truncation_reason_is_not_masked(response_shape, reasoning_size):
    choice = {
        "finish_reason": "length",
        "message": {"content": "", "reasoning_content": "r" * reasoning_size},
    }
    response = {"choices": [choice]}
    if response_shape == "object":
        response = SimpleNamespace(choices=[SimpleNamespace(
            finish_reason=choice["finish_reason"],
            message=SimpleNamespace(**choice["message"]),
        )])
    compressor = _compressor()
    compressor._previous_summary = "Last complete summary."
    with patch("agent.context_compressor.call_llm", return_value=response):
        out = compressor._generate_summary(_turns())
    assert out is None
    error = compressor._last_summary_error
    assert isinstance(error, str)
    assert "finish_reason=length" in error
    assert "reasoning_fallback_char_limit" not in error
    assert compressor._last_summary_truncated_failure is True
    assert compressor._previous_summary == "Last complete summary."


def test_empty_content_without_reasoning_still_fails():
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = None
    with patch("agent.context_compressor.call_llm", return_value=mock_response):
        out = _compressor()._generate_summary(_turns())
    assert out is None
