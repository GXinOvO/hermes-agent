"""The transport scope grants no identity and suppresses the generic send retries."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.platforms.base import SendResult
from gateway.strict_transport import strict_transport, strict_transport_scope
from tests.gateway.test_delivery_ledger_producer import _Adapter


@pytest.mark.asyncio
@pytest.mark.parametrize('error', ['HTTP 503 connection reset', 'invalid format', 'request timeout'])
async def test_strict_transport_never_retries_falls_back_or_sends_notice(error):
    adapter = _Adapter()
    adapter.send = AsyncMock(return_value=SendResult(success=False, error=error, retryable=True))
    adapter._send_plain_fallback = AsyncMock()
    with strict_transport_scope():
        result = await adapter._send_with_retry('chat', 'answer', base_delay=0)
        assert strict_transport() is True
    assert result.success is False
    assert adapter.send.await_count == 1
    adapter._send_plain_fallback.assert_not_awaited()
    assert strict_transport() is False


@pytest.mark.asyncio
async def test_transport_scope_does_not_leak_into_parallel_human_reply():
    strict_adapter, human_adapter = _Adapter(), _Adapter()
    strict_adapter.send = AsyncMock(return_value=SendResult(success=False, error='HTTP 503', retryable=True))
    human_adapter.send = AsyncMock(side_effect=[SendResult(success=False, error='HTTP 503', retryable=True),
                                               SendResult(success=True, message_id='human-ack')])

    async def strict_call():
        with strict_transport_scope():
            return await strict_adapter._send_with_retry('chat', 'automated', base_delay=0)

    strict_result, human_result = await asyncio.gather(strict_call(),
        human_adapter._send_with_retry('chat', 'human', base_delay=0))
    assert not strict_result.success and human_result.success
    assert strict_adapter.send.await_count == 1 and human_adapter.send.await_count == 2


def test_message_text_and_ids_cannot_mint_runtime_policy():
    from gateway.session_delivery_policy import recovery_descriptor, strict_event, mark_bot_request
    event = SimpleNamespace(text='strict automation consume_only', internal=True,
        message_id='bot:completion', metadata={'strict':True, 'result_recovery_v1':'provider_before_execution'},
        _automation_input_kind='provider_before_execution', _automation_final_claim={'strict':True})
    assert not strict_event(event)
    assert recovery_descriptor(event) == {'result_recovery_v1':'consume_only'}
    mark_bot_request(event)
    assert recovery_descriptor(event) == {'result_recovery_v1':'provider_before_execution'}
