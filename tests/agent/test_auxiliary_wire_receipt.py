"""Content-free receipts at the final auxiliary Chat Completions SDK boundary."""

import json
from types import SimpleNamespace

import pytest

from agent.auxiliary_client import (
    AuxiliaryExplicitCancellation, _acreate_with_progress, _create_with_progress_once, _relay_aux_call_scope,
    _set_relay_auxiliary_route,
    extract_content_or_reasoning,
)
from agent.auxiliary_wire_receipt import compression_attempt_scope, observe_stream_with_wire_receipt, run_with_wire_receipt


def _response():
    return SimpleNamespace(
        id="resp-wire-1",
        model="same-model",
        choices=[SimpleNamespace(
            message=SimpleNamespace(content="summary", reasoning_content="r" * 3),
            finish_reason="length",
        )],
        usage=SimpleNamespace(
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=0),
        ),
    )


def _receipts(caplog):
    records = []
    for record in caplog.records:
        prefix = "auxiliary wire receipt: "
        if record.getMessage().startswith(prefix):
            records.append(json.loads(record.getMessage()[len(prefix):]))
    return records


def test_sync_wire_receipt_carries_final_request_and_response_evidence(caplog):
    response = _response()
    client = SimpleNamespace(
        base_url="https://aux.invalid/v1",
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: response)),
    )
    kwargs = {
        "model": "same-model",
        "messages": [{"role": "user", "content": "summary"}],
        "max_tokens": 11,
        "max_completion_tokens": 12,
        "max_output_tokens": 13,
        "extra_body": {"reasoning": {"enabled": True}},
    }
    route_info = {}

    with caplog.at_level("INFO", logger="agent.auxiliary_wire"), _relay_aux_call_scope(
        ("compression",), {"route_info": route_info}
    ):
        _set_relay_auxiliary_route("custom", "same-model", "chat_completions")
        with compression_attempt_scope("compression-attempt-1"):
            result = _create_with_progress_once(client, kwargs, "compression")

    assert result is response
    [receipt] = _receipts(caplog)
    assert receipt["compression_attempt_id"] == "compression-attempt-1"
    assert receipt["auxiliary_request_id"].startswith("aux-")
    assert receipt["physical_attempt"] == 1
    assert receipt["route"]["provider"] == "custom"
    assert receipt["route"]["model"] == "same-model"
    assert len(receipt["route"]["endpoint_sha256"]) == 64
    assert receipt["max_fields"] == {
        "max_tokens": {"present": True, "value": 11},
        "max_completion_tokens": {"present": True, "value": 12},
        "max_output_tokens": {"present": True, "value": 13},
    }
    assert receipt["reasoning_fields"] == ["extra_body.reasoning"]
    assert receipt["response"]["id"] == "resp-wire-1"
    assert receipt["response"]["finish_reason_raw"] == "length"
    assert receipt["response"]["finish_reason"] == "length"
    assert receipt["response"]["usage"] == {
        "present": True,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "reasoning_tokens": 0,
    }
    assert receipt["response"]["visible_content_chars"] == len("summary")
    assert receipt["response"]["reasoning_chars"] == 3
    assert receipt["response"]["local_reasoning_clipped"] is False
    assert route_info["wire_receipt_ids"] == receipt["wire_receipt_id"]
    assert "summary" not in json.dumps(receipt)
    assert "aux.invalid" not in json.dumps(receipt)


@pytest.mark.asyncio
async def test_async_wire_receipt_uses_same_attempt_contract(caplog):
    from unittest.mock import AsyncMock

    response = _response()
    client = SimpleNamespace(
        base_url="https://async.invalid/v1",
        chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(return_value=response))),
    )
    with caplog.at_level("INFO", logger="agent.auxiliary_wire"), _relay_aux_call_scope(("compression",), {}):
        _set_relay_auxiliary_route("custom", "same-model", "chat_completions")
        with compression_attempt_scope("async-attempt-1"):
            result = await _acreate_with_progress(
                client,
                {"model": "same-model", "messages": [{"role": "user", "content": "x"}]},
                "compression",
            )
    assert result is response
    [receipt] = _receipts(caplog)
    assert receipt["compression_attempt_id"] == "async-attempt-1"
    assert receipt["physical_attempt"] == 1
    assert receipt["response"]["id"] == "resp-wire-1"


def test_reasoning_clip_updates_only_the_matching_compression_receipt(caplog):
    response = {
        "id": "resp-wire-2",
        "model": "same-model",
        "choices": [{"message": {"content": "", "reasoning_content": "r" * 8_001}, "finish_reason": "stop"}],
    }
    client = SimpleNamespace(
        base_url="https://aux.invalid/v1",
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: response)),
    )
    with caplog.at_level("INFO", logger="agent.auxiliary_wire"), _relay_aux_call_scope(("compression",), {}):
        _set_relay_auxiliary_route("custom", "same-model", "chat_completions")
        with compression_attempt_scope("clip-attempt-1"):
            _create_with_progress_once(client, {"model": "same-model", "messages": []}, "compression")
            assert extract_content_or_reasoning(response, max_reasoning_chars=8_000) == "r" * 8_000
    records = _receipts(caplog)
    assert records[0]["response"]["local_reasoning_clipped"] is False
    updates = [r for r in caplog.records if r.getMessage().startswith("auxiliary wire receipt update: ")]
    assert len(updates) == 1
    assert '"local_reasoning_clipped":true' in updates[0].getMessage()


def test_physical_retries_share_logical_request_id_and_increment_attempt(caplog):
    responses = iter([
        SimpleNamespace(id="resp-wire-3", model="same-model", choices=[SimpleNamespace(
            message=SimpleNamespace(content="first"), finish_reason="length",
        )]),
        SimpleNamespace(id="resp-wire-4", model="same-model", choices=[SimpleNamespace(
            message=SimpleNamespace(content="second"), finish_reason="stop",
        )]),
    ])
    client = SimpleNamespace(
        base_url="https://retry.invalid/v1",
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: next(responses))),
    )
    with caplog.at_level("INFO", logger="agent.auxiliary_wire"), _relay_aux_call_scope(("compression",), {}):
        _set_relay_auxiliary_route("custom", "same-model", "chat_completions")
        with compression_attempt_scope("retry-attempt-1"):
            _create_with_progress_once(client, {"model": "same-model", "messages": []}, "compression")
            _create_with_progress_once(client, {"model": "same-model", "messages": []}, "compression")
    receipts = _receipts(caplog)
    assert [item["physical_attempt"] for item in receipts] == [1, 2]
    assert len({item["auxiliary_request_id"] for item in receipts}) == 1
    assert all(item["compression_attempt_id"] == "retry-attempt-1" for item in receipts)


def test_raw_stream_path_finishes_receipt_after_consumer_exhausts_stream(caplog):
    def chunk(text, finish=None):
        return SimpleNamespace(
            id="stream-response", model="same-model", usage=None,
            choices=[SimpleNamespace(
                delta=SimpleNamespace(content=text, reasoning=None, reasoning_details=None, tool_calls=None),
                finish_reason=finish,
            )],
        )

    client = SimpleNamespace(base_url="https://stream.invalid/v1")
    with caplog.at_level("INFO", logger="agent.auxiliary_wire"), _relay_aux_call_scope(("moa_aggregator",), {}):
        _set_relay_auxiliary_route("custom", "same-model", "chat_completions")
        with compression_attempt_scope("raw-stream-attempt"):
            stream = observe_stream_with_wire_receipt(
                client,
                {"model": "same-model", "messages": [], "stream": True},
                lambda: iter([chunk("a"), chunk("b", "stop")]),
            )
            assert [part.choices[0].delta.content for part in stream] == ["a", "b"]
    [receipt] = _receipts(caplog)
    assert receipt["request_stream"] is True
    assert receipt["response"]["id"] == "stream-response"
    assert receipt["response"]["finish_reason"] == "stop"


@pytest.mark.parametrize("consume_first", [False, True])
def test_raw_stream_early_close_closes_underlying_stream(caplog, consume_first):
    class _Stream:
        def __init__(self):
            self.closed = 0

        def __iter__(self):
            return self

        def __next__(self):
            return chunk("first")

        def close(self):
            self.closed += 1

    def chunk(text):
        return SimpleNamespace(
            id="early-close-response", model="same-model", usage=None,
            choices=[SimpleNamespace(
                delta=SimpleNamespace(content=text, reasoning=None, reasoning_details=None, tool_calls=None),
                finish_reason=None,
            )],
        )

    stream = _Stream()
    client = SimpleNamespace(base_url="https://early-close.invalid/v1")
    with caplog.at_level("INFO", logger="agent.auxiliary_wire"), compression_attempt_scope("early-close-attempt"):
        observed = observe_stream_with_wire_receipt(
            client, {"model": "same-model", "messages": [], "stream": True}, lambda: stream,
        )
        if consume_first:
            next(observed)
        observed.close()
        observed.close()
    assert stream.closed == 1
    records = _receipts(caplog)
    assert len(records) == 1 and records[0]["status"] == "error"
    assert records[0]["error"]["type"] == "GeneratorExit"


def test_receipt_uses_effective_extra_body_wire_fields(caplog):
    response = _response()
    client = SimpleNamespace(
        base_url="https://wire.invalid/v1",
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: response)),
    )
    request = {
        "model": "requested-model",
        "messages": [],
        "extra_body": {"model": "wire-model", "max_tokens": 7},
    }
    with caplog.at_level("INFO", logger="agent.auxiliary_wire"):
        from agent.auxiliary_wire_receipt import run_with_wire_receipt
        run_with_wire_receipt(client, request, lambda: client.chat.completions.create(**request))
    [receipt] = _receipts(caplog)
    assert receipt["route"]["model"] == "wire-model"
    assert receipt["max_fields"]["max_tokens"] == {"present": True, "value": 7}


def test_structured_finish_metadata_is_redacted_to_a_scalar(caplog):
    response = SimpleNamespace(
        id="resp-safe", model="same-model",
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok"), finish_reason={"body": "SECRET"})],
    )
    client = SimpleNamespace(base_url="https://safe.invalid/v1")
    with caplog.at_level("INFO", logger="agent.auxiliary_wire"):
        from agent.auxiliary_wire_receipt import run_with_wire_receipt
        run_with_wire_receipt(client, {"model": "same-model", "messages": []}, lambda: response)
    assert "SECRET" not in caplog.text
    [receipt] = _receipts(caplog)
    assert receipt["response"]["finish_reason_raw"] == "<dict>"


def test_anthropic_stream_fallback_emits_one_receipt_per_native_sdk_call(caplog):
    from unittest.mock import patch
    from agent.auxiliary_client import _AnthropicCompletionsAdapter

    class _BrokenStream:
        def __enter__(self):
            raise RuntimeError("Unexpected event order, got content_block_delta before message_start")

        def __exit__(self, *_args):
            return False

    response = SimpleNamespace(id="anthropic-create", model="m", content=[], stop_reason="end_turn", usage=None)
    messages = SimpleNamespace(
        stream=lambda **_kwargs: _BrokenStream(),
        create=lambda **_kwargs: response,
    )
    client = SimpleNamespace(messages=messages, base_url="https://anthropic.invalid/v1")
    adapter = _AnthropicCompletionsAdapter(client, "m", base_url=client.base_url)
    with caplog.at_level("INFO", logger="agent.auxiliary_wire"), _relay_aux_call_scope(("compression",), {}):
        _set_relay_auxiliary_route("custom", "m", "anthropic_messages")
        with compression_attempt_scope("anthropic-fallback-attempt"):
            with patch("agent.anthropic_adapter.build_anthropic_kwargs", return_value={
                "model": "m", "messages": [], "max_tokens": 8,
            }):
                adapter.create(messages=[])
    receipts = _receipts(caplog)
    assert len(receipts) == 2
    assert [receipt["request_stream"] for receipt in receipts] == [True, False]


def test_anthropic_native_stream_final_message_is_read_once(caplog):
    from unittest.mock import patch
    from agent.auxiliary_client import _AnthropicCompletionsAdapter

    message = SimpleNamespace(id="message-1", model="m", content=[], stop_reason="end_turn", usage=None)

    class _Stream:
        def __init__(self):
            self.final_calls = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            return iter([SimpleNamespace(type="message_stop")])

        def get_final_message(self):
            self.final_calls += 1
            return message

    stream = _Stream()
    client = SimpleNamespace(
        base_url="https://anthropic-once.invalid/v1",
        messages=SimpleNamespace(stream=lambda **_kwargs: stream, create=lambda **_kwargs: message),
    )
    adapter = _AnthropicCompletionsAdapter(client, "m", base_url=client.base_url)
    with caplog.at_level("INFO", logger="agent.auxiliary_wire"), _relay_aux_call_scope(("compression",), {}):
        _set_relay_auxiliary_route("custom", "m", "anthropic_messages")
        with compression_attempt_scope("anthropic-once-attempt"), patch(
            "agent.anthropic_adapter.build_anthropic_kwargs",
            return_value={"model": "m", "messages": [], "max_tokens": 8},
        ):
            adapter.create(messages=[])
    assert stream.final_calls == 1


def test_reused_anthropic_adapter_prefers_current_relay_context(caplog):
    from unittest.mock import patch
    from agent.auxiliary_client import _AnthropicCompletionsAdapter

    response = SimpleNamespace(id="reused", model="m", content=[], stop_reason="end_turn", usage=None)
    client = SimpleNamespace(
        base_url="https://anthropic-reused.invalid/v1",
        messages=SimpleNamespace(create=lambda **_kwargs: response),
    )
    stale_context = {
        "request_id": "old-request", "provider": "old-provider", "model": "old-model",
        "api_mode": "anthropic_messages", "wire_receipt_ids": [],
    }
    adapter = _AnthropicCompletionsAdapter(client, "m", base_url=client.base_url, logical_context=stale_context)
    with caplog.at_level("INFO", logger="agent.auxiliary_wire"), _relay_aux_call_scope(("compression",), {}):
        _set_relay_auxiliary_route("new-provider", "m", "anthropic_messages")
        with compression_attempt_scope("reused-context-attempt"), patch(
            "agent.anthropic_adapter.build_anthropic_kwargs",
            return_value={"model": "m", "messages": [], "max_tokens": 8},
        ):
            adapter.create(messages=[])
    [receipt] = _receipts(caplog)
    assert receipt["auxiliary_request_id"].startswith("aux-")
    assert receipt["route"]["provider"] == "new-provider"


def test_bedrock_receipt_covers_each_converse_physical_attempt(caplog):
    from unittest.mock import patch
    from agent.auxiliary_client import _BedrockCompletionsAdapter

    raw_response = {
        "output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": 3, "outputTokens": 2, "totalTokens": 5},
        "modelId": "m",
    }

    class _Runtime:
        def __init__(self):
            self.calls = []

        def converse(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                raise RuntimeError("cache point rejected")
            return raw_response

    runtime = _Runtime()
    client = SimpleNamespace(base_url="https://bedrock.invalid")
    adapter = _BedrockCompletionsAdapter(
        "us-east-1", "m", logical_context=None, receipt_client=client,
    )
    with caplog.at_level("INFO", logger="agent.auxiliary_wire"), _relay_aux_call_scope(("compression",), {}):
        _set_relay_auxiliary_route("bedrock", "m", "bedrock_converse")
        with compression_attempt_scope("bedrock-attempt"):
            with patch("agent.bedrock_adapter._get_bedrock_runtime_client", return_value=runtime), patch(
                "agent.bedrock_adapter.recover_from_cache_point_rejection",
                return_value={"modelId": "m", "messages": [], "inferenceConfig": {"maxTokens": 7}},
            ):
                adapter.create(model="m", messages=[], max_tokens=7)
    receipts = _receipts(caplog)
    assert len(receipts) == 2
    assert [receipt["physical_attempt"] for receipt in receipts] == [1, 2]
    assert all(receipt["route"]["api_mode"] == "bedrock_converse" for receipt in receipts)
    assert all(receipt["max_fields"]["max_tokens"]["value"] == 7 for receipt in receipts)


@pytest.mark.asyncio
async def test_stream_open_cancellation_finishes_receipt(caplog):
    import asyncio
    from agent.auxiliary_client import _acreate_with_progress, aux_progress_hook
    from unittest.mock import AsyncMock

    client = SimpleNamespace(
        base_url="https://cancel.invalid/v1",
        chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(side_effect=asyncio.CancelledError))),
    )
    with caplog.at_level("INFO", logger="agent.auxiliary_wire"), aux_progress_hook(lambda: None):
        with compression_attempt_scope("cancel-attempt"):
            with pytest.raises(asyncio.CancelledError):
                await _acreate_with_progress(
                    client, {"model": "same-model", "messages": []}, "compression", force_stream=True,
                )
    records = _receipts(caplog)
    assert len(records) == 1 and records[0]["status"] == "error"


@pytest.mark.asyncio
async def test_async_stream_open_cancellation_does_not_fallback(caplog):
    import asyncio
    from unittest.mock import AsyncMock
    from agent.auxiliary_client import aux_progress_hook

    create = AsyncMock(side_effect=asyncio.CancelledError)
    client = SimpleNamespace(
        base_url="https://cancel-async-fallback.invalid/v1",
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    )
    with caplog.at_level("INFO", logger="agent.auxiliary_wire"), aux_progress_hook(lambda: None):
        with compression_attempt_scope("cancel-async-fallback-attempt"):
            with pytest.raises(asyncio.CancelledError):
                await _acreate_with_progress(
                    client, {"model": "same-model", "messages": []}, "compression",
                )
    records = _receipts(caplog)
    assert create.await_count == 1
    assert len(records) == 1 and records[0]["status"] == "error"


def test_sync_stream_open_cancellation_finishes_receipt(caplog):
    def create(**_kwargs):
        raise AuxiliaryExplicitCancellation()

    client = SimpleNamespace(
        base_url="https://cancel-sync.invalid/v1",
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    )
    with caplog.at_level("INFO", logger="agent.auxiliary_wire"), _relay_aux_call_scope(("compression",), {}):
        with compression_attempt_scope("cancel-sync-attempt"):
            with pytest.raises(AuxiliaryExplicitCancellation):
                _create_with_progress_once(
                    client, {"model": "same-model", "messages": []}, "compression", force_stream=True,
                )
    records = _receipts(caplog)
    assert len(records) == 1 and records[0]["status"] == "error"


def test_receipt_observer_logging_failure_is_fail_open(caplog):
    import logging

    class _BrokenHandler(logging.Handler):
        def emit(self, record):
            del record
            raise OSError("observer handler failed")

    logger = logging.getLogger("agent.auxiliary_wire")
    handler = _BrokenHandler()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        response = _response()
        client = SimpleNamespace(base_url="https://handler.invalid/v1")
        result = run_with_wire_receipt(
            client, {"model": "same-model", "messages": []}, lambda: response,
        )
    finally:
        logger.removeHandler(handler)
        handler.close()
    assert result is response
