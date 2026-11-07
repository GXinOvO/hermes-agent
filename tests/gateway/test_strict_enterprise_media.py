"""Real SDK media submissions, fail-stop batches, stream drain and ledger receipts."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.strict_transport import strict_transport_scope, supports_strict_transport
from tests.gateway.test_strict_enterprise_transport import peer, feishu, wecom, dingtalk, google_chat, teams, message_posts


@asynccontextmanager
async def platform_peer(name, wire):
    if name == 'wecom':
        async with wecom(wire, passive=False) as adapter:
            yield adapter, 'chat'
    else:
        async with {'feishu': feishu, 'dingtalk': dingtalk, 'google_chat': google_chat, 'teams': teams}[name](wire) as adapter:
            yield adapter, 'spaces/peer' if name == 'google_chat' else 'chat'


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['feishu', 'dingtalk', 'google_chat', 'teams', 'wecom'])
@pytest.mark.parametrize('fault', [None, 'lost'])
async def test_native_text_success_and_lost_ack_single_submit(name, fault):
    paths = {'feishu': '/open-apis/im/v1/messages', 'dingtalk': '/ding', 'google_chat': '/v1/spaces/peer/messages',
             'teams': '/v3/conversations/chat/activities', 'wecom': 'aibot_send_msg'}
    path = paths[name]
    async with peer({path: fault} if fault else {}) as wire, platform_peer(name, wire) as (adapter, chat):
        assert supports_strict_transport(adapter)
        with strict_transport_scope():
            result = await adapter.send(chat, 'sealed final')
        assert result.success is (fault is None)
        assert len([row for row in wire.events if row[1] == path]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['image_file', 'document', 'voice', 'video'])
@pytest.mark.parametrize('name,fault_at', [(name, fault) for name in ['feishu', 'teams', 'wecom', 'google_chat']
                                         for fault in [None, 'upload', 'final'] if (name, fault) != ('teams', 'upload')])
async def test_native_media_success_upload_lost_ack_and_final_lost_ack(name, kind, fault_at, tmp_path):
    extensions = {'image_file': '.png', 'document': '.txt', 'voice': '.ogg' if name == 'feishu' else '.amr', 'video': '.mp4'}
    path = tmp_path / ('proof' + extensions[kind])
    await asyncio.to_thread(path.write_bytes, b'\x89PNG\r\n\x1a\nproof' if kind == 'image_file' else b'#!AMR\nproof' if kind == 'voice' else b'proof')
    upload = {'feishu': '/open-apis/im/v1/images' if kind == 'image_file' else '/open-apis/im/v1/files',
              'wecom': 'aibot_upload_media_init', 'google_chat': '/upload/v1/spaces/peer/attachments:upload'}
    final = {'feishu': '/open-apis/im/v1/messages', 'teams': '/v3/conversations/chat/activities',
             'wecom': 'aibot_send_msg', 'google_chat': '/v1/spaces/peer/messages'}
    failure_path = upload.get(name) if fault_at == 'upload' else final[name] if fault_at == 'final' else None
    async with peer({failure_path: 'lost'} if failure_path else {}) as wire, platform_peer(name, wire) as (adapter, chat):
        if name == 'google_chat':
            adapter._user_credentials = adapter._credentials
            adapter._user_chat_api = adapter._chat_api
        method = 'send_' + kind
        assert supports_strict_transport(adapter, method)
        key = {'image_file': 'image_path', 'document': 'file_path', 'voice': 'audio_path', 'video': 'video_path'}[kind]
        with strict_transport_scope():
            result = await getattr(adapter, method)(chat_id=chat, **{key: str(path)}, caption='proof')
        assert result.success is (fault_at is None), (result, wire.events)
        finals = [row for row in wire.events if row[1] == final[name]]
        assert len(finals) == (0 if fault_at == 'upload' else 2 if name == 'wecom' and fault_at is None else 1), wire.events
        if name in upload:
            assert len([row for row in wire.events if row[1] == upload[name]]) == 1
        assert not any(row[0] == 'PATCH' for row in wire.events)


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['feishu', 'dingtalk', 'google_chat', 'teams', 'wecom'])
@pytest.mark.parametrize('fault', [None, 'lost'])
async def test_image_batch_stops_at_first_unknown(name, fault, tmp_path):
    path = tmp_path / 'proof.png'
    await asyncio.to_thread(path.write_bytes, b'\x89PNG\r\n\x1a\nproof')
    final = {'feishu': '/open-apis/im/v1/messages', 'dingtalk': '/ding', 'google_chat': '/v1/spaces/peer/messages',
             'teams': '/v3/conversations/chat/activities', 'wecom': 'aibot_send_msg'}[name]
    async with peer({final: fault} if fault else {}) as wire, platform_peer(name, wire) as (adapter, chat):
        image = 'https://example.invalid/proof.png' if name in {'dingtalk', 'google_chat'} else path.as_uri()
        assert supports_strict_transport(adapter, 'send_multiple_images')
        with strict_transport_scope():
            result = await adapter.send_multiple_images(chat, [(image, 'first'), (image, 'second')])
        assert result.success is (fault is None), (result, wire.events)
        assert len([row for row in wire.events if row[1] == final]) == ((4 if name == 'wecom' else 2) if fault is None else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['feishu', 'dingtalk', 'google_chat', 'teams', 'wecom'])
async def test_strict_stream_buffers_real_adapter_then_ledger_unknown_never_resends(name, tmp_path):
    from gateway.config import Platform
    from gateway.platforms.event import MessageEvent
    from gateway.session import SessionSource
    from gateway.session_delivery_policy import automation_delivery_scope
    from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
    from gateway import pending_final as pf
    from gateway.final_delivery import send_text
    from gateway.session_results import finish_result
    from hermes_state import SessionDB
    from hermes_state_runtime import begin_runtime_epoch, admit_session_input, claim_session_input
    final = {'feishu': '/open-apis/im/v1/messages', 'dingtalk': '/ding', 'google_chat': '/v1/spaces/peer/messages',
             'teams': '/v3/conversations/chat/activities', 'wecom': 'aibot_send_msg'}[name]
    async with peer({final: 'lost'}) as wire, platform_peer(name, wire) as (adapter, chat):
        db = SessionDB(tmp_path / 'admission.db')
        db.create_session('sid', source=name)
        epoch = begin_runtime_epoch(db, instance_id='owner')
        route = f'agent:main:{name}:dm:{chat}'
        source = SessionSource(platform=Platform(name), chat_id=chat, chat_type='dm')
        payload = {'text': 'work', 'native_text_v1': {'route': route, 'source': source.to_dict(),
                   'automation': {'owner': 'sid', 'identity': 'saved-result', 'result_recovery_v1': 'consume_only'}}}
        admit_session_input(db, epoch=epoch, principal_id='automation:' + route, session_id='sid', request_id='saved-result', payload=payload)
        row = claim_session_input(db, epoch=epoch, session_id='sid')
        event = MessageEvent(text='work', source=source, internal=True, message_id='saved-result')
        authority = SimpleNamespace(db=db, runner=SimpleNamespace(_adapter_for_source=lambda source: adapter))
        try:
            async with automation_delivery_scope(authority, row, event):
                consumer = GatewayStreamConsumer(adapter, chat, StreamConsumerConfig(transport='native' if name == 'wecom' else 'edit', cursor=''))
                consumer.on_delta('unsealed preview')
                consumer.on_delta(None)
                consumer.on_commentary('interim segment')
                consumer.on_delta('another preview')
                consumer.finish('sealed final')
                await consumer.run()
                assert not consumer.final_content_delivered and not consumer.already_sent
                assert wire.events == []
                prepared = await pf.prepare(authority, row, event, route, 'sealed final')
            assert prepared is not None
            finish_result(db, epoch=epoch, row=row, response='sealed final', outcome='completed',
                          result={'result': {'final_response': 'sealed final', 'messages': [], '_pending_final_v1': prepared}, 'usage': {}})
            result = await send_text(adapter, event, route, 'sealed final', reply_to=None, metadata=prepared['metadata'])
            assert not result.success
            record = pf.pending(db)[0]['parts']['text']
            assert (record['state'], record['attempts']) == ('unknown', 1)
            before = list(wire.events)
            result = await send_text(adapter, event, route, 'sealed final', reply_to=None, metadata=prepared['metadata'])
            assert not result.success and wire.events == before
            assert len([row for row in wire.events if row[1] == final]) == 1
        finally:
            db.close()
