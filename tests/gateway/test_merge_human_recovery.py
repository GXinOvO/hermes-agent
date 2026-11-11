from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway import pending_final
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from gateway.session_results import finish_result
from gateway.strict_transport import strict_call
from hermes_state import SessionDB
from hermes_state_runtime import admit_session_input, begin_runtime_epoch, claim_session_input
from plugins.platforms.matrix.adapter import MatrixAdapter
from tests.gateway import test_pending_final as fixtures
from tests.gateway.test_pending_final_original_identity import settle


@pytest.mark.asyncio
async def test_human_matrix_recovery_keeps_encrypted_transport(tmp_path, monkeypatch):
    adapter = MatrixAdapter(PlatformConfig(enabled=True, extra={}))
    adapter._encryption = True
    adapter._client = SimpleNamespace(crypto=object())
    adapter._send_room_message = AsyncMock(return_value='$local-ack')
    source = SessionSource(platform=Platform.MATRIX, chat_id='!local:example')
    route = 'agent:main:matrix:dm:!local:example'
    payload = {'text': 'question', 'native_text_v1': {
        'route': route, 'source': {**source.to_dict(), 'is_bot': False}}}

    direct = await adapter.send(source.chat_id, 'control')
    assert direct.success
    adapter._send_room_message.reset_mock()
    strict = await strict_call(adapter, 'send', chat_id=source.chat_id, content='control')
    assert not strict.success and 'E2EE' in strict.error
    adapter._send_room_message.assert_not_awaited()

    with SessionDB(tmp_path / 'state.db') as db:
        db.create_session('sid', source='matrix')
        epoch = begin_runtime_epoch(db, instance_id='owner')
        admit_session_input(db, epoch=epoch, principal_id='messaging:sender',
                            session_id='sid', request_id='question', payload=payload)
        row = claim_session_input(db, epoch=epoch, session_id='sid')
        event = MessageEvent(text='question', source=source, message_id='question')
        authority = SimpleNamespace(db=db, runner=SimpleNamespace(_adapter_for_source=lambda source: adapter))
        descriptor = await pending_final.prepare(authority, row, event, route, 'sealed answer')
        finish_result(db, epoch=epoch, row=row, response='sealed answer', outcome='completed',
                      result={'result': {'final_response': 'sealed answer', 'messages': [],
                                         '_pending_final_v1': descriptor}, 'usage': {}})
        assert pending_final.pending(db)[0]['binding']['policy'] == 'human_reply_v1'
        monkeypatch.setattr('gateway.delivery_ledger._owner_alive', lambda *args: False)
        monkeypatch.setattr('gateway.session_authorities.owner_scope', lambda authority: nullcontext())

        async def check_route(runner, payload, session, saved_source, current_adapter):
            return saved_source, route

        monkeypatch.setattr('gateway.session_envelope.check_native_route', check_route)
        runner = SimpleNamespace(session_authority=authority, _delivery_adapter_for=lambda source: adapter,
                                 session_store=SimpleNamespace(lookup_by_session_key=lambda route:
                                                               SimpleNamespace(session_id='sid', suspended=False)))
        delivered = await pending_final.recover(runner)
        part = pending_final.pending(db)[0]['parts']['text']
        assert delivered == 1, part
        adapter._send_room_message.assert_awaited_once()
        assert part['state'] == 'delivered'


@pytest.mark.asyncio
@pytest.mark.parametrize('invalidate', ['none', 'after_claim', 'during_send', 'during_handoff'])
async def test_human_recovery_does_not_queue_invalidated_reply(tmp_path, monkeypatch, invalidate):
    from gateway import delivery_ledger
    from hermes_state_background import invalidate_target

    db, epoch, row, _event, descriptor, authority = await fixtures._sealed(tmp_path, human=True)
    adapter = fixtures.Adapter()
    adapter.gateway_runner = SimpleNamespace(_schedule_flood_redelivery=lambda *args, **kwargs: None)
    adapter._final_delivery_adapter = lambda source: adapter
    sent = []

    async def send(**kwargs):
        sent.append(kwargs['content'])
        if invalidate == 'during_send':
            db._execute_write(lambda conn: invalidate_target(conn, 'sid'))
        return SendResult(success=False, error='send_path_degraded')

    adapter._send_with_retry = send
    monkeypatch.setattr(delivery_ledger, '_db_path', lambda: tmp_path / 'outbound.db')
    monkeypatch.setattr(delivery_ledger, '_owner_alive', lambda *args: False)
    monkeypatch.setattr('gateway.session_authorities.owner_scope', lambda authority: nullcontext())

    async def check_route(runner, payload, session, source, current_adapter):
        return source, descriptor['binding']['route']

    monkeypatch.setattr('gateway.session_envelope.check_native_route', check_route)
    original_claim = pending_final.claim

    def claim(*args, **kwargs):
        value, won, state = original_claim(*args, **kwargs)
        if won and invalidate == 'after_claim':
            db._execute_write(lambda conn: invalidate_target(conn, 'sid'))
        return value, won, state

    monkeypatch.setattr(pending_final, 'claim', claim)
    original_handoff = delivery_ledger.record_human_final_failure

    def handoff(*args, **kwargs):
        if invalidate == 'during_handoff':
            db._execute_write(lambda conn: invalidate_target(conn, 'sid'))
        return original_handoff(*args, **kwargs)

    monkeypatch.setattr(delivery_ledger, 'record_human_final_failure', handoff)
    runner = SimpleNamespace(session_authority=authority, _delivery_adapter_for=lambda source: adapter,
                             session_store=SimpleNamespace(lookup_by_session_key=lambda route:
                                                           SimpleNamespace(session_id='sid', suspended=False)))
    try:
        settle(db, epoch, row, descriptor)
        assert await pending_final.recover(runner) == 0
        with delivery_ledger._connect() as conn:
            rows = conn.execute('SELECT state, content FROM delivery_obligations').fetchall()
        assert sent == ([] if invalidate == 'after_claim' else ['sealed final'])
        assert rows == ([('failed', 'sealed final')] if invalidate == 'none' else [])
        assert pending_final.pending(db)[0]['parts']['text']['state'] == 'unknown'
    finally:
        db.close()