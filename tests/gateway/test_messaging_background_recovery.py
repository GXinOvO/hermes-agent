from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.session import SessionSource
from gateway.strict_transport import single_attempt_transport
from hermes_state import SessionDB


@pytest.mark.asyncio
async def test_saved_result_is_delivered_once_and_failed_ack_is_unknown(tmp_path):
    from gateway import background_messaging_results as durable
    from hermes_state_background import complete
    db = SessionDB(tmp_path / 'state.db')
    adapter = SimpleNamespace(send=single_attempt_transport(AsyncMock(return_value=SendResult(success=True))),
        extract_media=lambda text:([], text), extract_images=lambda text:([], text))
    runner = SimpleNamespace(_session_db=db, _delivery_adapter_for=lambda source:adapter)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id='original')
    try:
        context = await durable.prepare(runner, 'read only', source, 'bg_sealed', [], [])
        complete(db, 'bg_sealed', context[1], {'task_id':'bg_sealed', 'text':'saved', 'header':'done: ', 'metadata':{}})
        assert await durable.recover(runner, db) == 1
        assert await durable.recover(runner, db) == 0
        adapter.send.assert_awaited_once_with(chat_id='original', content='done: saved', metadata={})
        context = await durable.prepare(runner, 'read only', source, 'bg_failed', [], [])
        complete(db, 'bg_failed', context[1], {'task_id':'bg_failed', 'text':'saved', 'header':'done: ', 'metadata':{}})
        adapter.send.return_value = SendResult(success=False, error='lost acknowledgement')
        assert await durable.recover(runner, db) == 0
        assert await durable.recover(runner, db) == 0
        assert adapter.send.await_count == 2
    finally:
        db.close()


@pytest.mark.asyncio
async def test_inflight_or_partial_delivery_is_never_restarted(tmp_path):
    from gateway import background_messaging_results as durable
    from hermes_state_background import complete, claim_delivery
    db = SessionDB(tmp_path / 'state.db')
    runner = SimpleNamespace(_session_db=db, _delivery_adapter_for=lambda source:None)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id='original')
    try:
        context = await durable.prepare(runner, 'read only', source, 'bg_running', [], [])
        context = await durable.prepare(runner, 'read only', source, 'bg_attempted', [], [])
        complete(db, 'bg_attempted', context[1], {'task_id':'bg_attempted', 'text':'saved'})
        assert claim_delivery(db, 'bg_attempted')
        assert await durable.recover(runner, db) == 0
        with pytest.raises(ValueError, match='already exists'):
            await durable.prepare(runner, 'read only', source, 'bg_running', [], [])
        with pytest.raises(ValueError, match='identity conflict'):
            await durable.prepare(runner, 'read only', SessionSource(platform=Platform.TELEGRAM, chat_id='changed'),
                                  'bg_attempted', [], [])
    finally:
        db.close()
