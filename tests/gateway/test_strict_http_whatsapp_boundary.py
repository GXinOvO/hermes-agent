"""Baileys stays explicitly unverified; human bridge HTTP behavior is preserved."""
import aiohttp
import pytest

from gateway.config import PlatformConfig
from gateway.strict_transport import strict_call, supports_strict_transport
from tests.gateway import test_strict_http_peers as peers


@pytest.fixture
def automation_input(tmp_path):
    from gateway.config import Platform
    from gateway.platforms.event import MessageEvent
    from gateway.session import SessionSource
    from hermes_state import SessionDB
    from hermes_state_runtime import admit_session_input, begin_runtime_epoch, claim_session_input

    with SessionDB(tmp_path / 'state.db') as db:
        db.create_session('sid', source='whatsapp')
        epoch = begin_runtime_epoch(db, instance_id='owner')
        route = 'agent:main:whatsapp:dm:15550000001'
        source = SessionSource(platform=Platform.WHATSAPP, chat_id='15550000001', chat_type='dm')
        payload = {'text': 'work', 'native_text_v1': {'route': route, 'source': source.to_dict(),
                   'automation': {'owner': 'sid', 'identity': 'saved-result', 'result_recovery_v1': 'consume_only'}}}
        admit_session_input(db, epoch=epoch, principal_id='automation:' + route, session_id='sid',
                            request_id='saved-result', payload=payload)
        row = claim_session_input(db, epoch=epoch, session_id='sid')
        event = MessageEvent(text='work', source=source, internal=True, message_id='saved-result')
        yield db, row, event, route


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['send', 'send_image_file', 'send_document', 'send_voice', 'send_video'])
async def test_unverified_bridge_refused_before_http(method, tmp_path, automation_input):
    from gateway.final_delivery import send_text
    from gateway.session_delivery_policy import automation_delivery_scope, claim_for_event
    from plugins.platforms.whatsapp.adapter import WhatsAppAdapter
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch
    async with peers.http_peer() as peer, aiohttp.ClientSession() as client:
        adapter = WhatsAppAdapter(PlatformConfig(extra={'bridge_port': peer.port}))
        adapter._http_session = client
        assert not supports_strict_transport(adapter, method)
        with pytest.raises(RuntimeError, match='strict_transport_unverified'):
            await strict_call(adapter, method)
        if method == 'send':
            db, row, event, route = automation_input
            claim = AsyncMock()
            async with automation_delivery_scope(SimpleNamespace(db=db), row, event):
                runtime_claim = claim_for_event(event)
                assert runtime_claim is not None and runtime_claim.admission == row['admission_id']
                with patch('gateway.final_delivery._claim', claim):
                    result = await send_text(adapter, event, route, 'answer', reply_to=None, metadata=None)
            assert not result.success and 'pending' in result.error
            claim.assert_not_awaited()
        assert peer.records == []


@pytest.mark.asyncio
@pytest.mark.parametrize('method,key', [('send', 'content'), ('send_image_file', 'image_path'),
    ('send_document', 'file_path'), ('send_voice', 'audio_path'), ('send_video', 'video_path')])
async def test_human_bridge_explicit_retry_is_real_http(method, key, tmp_path):
    from plugins.platforms.whatsapp.adapter import WhatsAppAdapter
    local = tmp_path / 'media.ogg'
    local.write_bytes(b'media')
    async with peers.http_peer(lambda r, records: 'drop' if len(records) == 1 else None) as peer:
        async with aiohttp.ClientSession() as client:
            adapter = WhatsAppAdapter(PlatformConfig(extra={'bridge_port': peer.port}))
            adapter._http_session = client
            adapter._running = True
            kwargs = {'chat_id': '15550000001', key: 'answer' if key == 'content' else str(local)}
            first = await getattr(adapter, method)(**kwargs)
            second = await getattr(adapter, method)(**kwargs)
            assert not first.success and second.success and len(peer.records) == 2
            assert all(r.path == ('/send' if method == 'send' else '/send-media') for r in peer.records)
