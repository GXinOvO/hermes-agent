"""Branch receipts beyond the primary text/media matrix."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from gateway.config import PlatformConfig
from gateway.strict_transport import strict_transport_scope
from tests.gateway.test_strict_enterprise_transport import peer, wecom, feishu, dingtalk, teams, google_chat
from tests.gateway.test_strict_enterprise_media import platform_peer


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['feishu', 'wecom', 'dingtalk', 'google_chat', 'teams'])
@pytest.mark.parametrize('method', ['send_image', 'send_animation'])
@pytest.mark.parametrize('fault', [None, 'lost'])
async def test_remote_image_and_animation_real_download_submit(name, method, fault, monkeypatch):
    from tools import url_safety
    monkeypatch.setenv('HERMES_ALLOW_PRIVATE_URLS', 'true')
    url_safety._reset_allow_private_cache()
    final = {'feishu': '/open-apis/im/v1/messages', 'wecom': 'aibot_send_msg', 'dingtalk': '/ding',
             'google_chat': '/v1/spaces/peer/messages', 'teams': '/v3/conversations/chat/activities'}[name]
    try:
        async with peer({final: fault} if fault else {}) as wire, platform_peer(name, wire) as (adapter, chat):
            source = wire.base + ('/proof.gif' if method == 'send_animation' else '/proof.png')
            with strict_transport_scope():
                result = await getattr(adapter, method)(chat_id=chat, **{'animation_url' if method == 'send_animation' else 'image_url': source})
            assert result.success is (fault is None), (result, wire.events)
            assert len([row for row in wire.events if row[1] == final]) == 1
            if name in {'feishu', 'wecom'}:
                assert len([row for row in wire.events if row[0] == 'GET']) == 1
    finally:
        url_safety._reset_allow_private_cache()


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['feishu', 'wecom', 'google_chat', 'teams'])
async def test_chunk_ack_loss_stops_after_first_submit(name):
    path = {'feishu': '/open-apis/im/v1/messages', 'wecom': 'aibot_send_msg',
            'google_chat': '/v1/spaces/peer/messages', 'teams': '/v3/conversations/chat/activities'}[name]
    async with peer({path: 'lost'}) as wire, platform_peer(name, wire) as (adapter, chat):
        adapter.MAX_MESSAGE_LENGTH = 100
        with strict_transport_scope():
            result = await adapter.send(chat, 'one two three four five ' * 100)
        assert not result.success
        assert len([row for row in wire.events if row[1] == path]) == 1


@pytest.mark.asyncio
async def test_wecom_queue_reuses_worker_without_inheriting_prior_turn_scope():
    wire = SimpleNamespace(events=[], faults={})
    async with wecom(wire) as adapter:
        assert (await adapter.send('chat', 'human first')).success
        wire.events.clear()
        wire.faults['aibot_respond_msg'] = 500
        with strict_transport_scope():
            assert not (await adapter.send('chat', 'strict second')).success
        assert len(wire.events) == 1
        wire.events.clear()
        wire.faults['aibot_respond_msg'] = 500
        assert (await adapter.send('chat', 'human third')).success
        assert [row[1] for row in wire.events] == ['aibot_respond_msg', 'aibot_send_msg']


@pytest.mark.asyncio
async def test_wecom_caption_failure_stops_downgrade_notice_and_next_batch_image(tmp_path):
    # Unsupported voice MIME degrades to a file and ordinarily emits two followups.
    path = tmp_path / 'proof.ogg'
    await asyncio.to_thread(path.write_bytes, b'OggSproof')
    wire = SimpleNamespace(events=[], faults={'aibot_send_msg': [None, 'lost']})
    async with wecom(wire, passive=False) as adapter:
        with strict_transport_scope():
            result = await adapter.send_voice('chat', str(path), caption='caption')
        assert not result.success
        assert [row[2]['msgtype'] for row in wire.events if row[1] == 'aibot_send_msg'] == ['file', 'markdown']


@pytest.mark.asyncio
async def test_wecom_seeded_native_stream_is_not_finalized_by_strict_plain_send():
    wire = SimpleNamespace(events=[], faults={})
    async with wecom(wire) as adapter:
        assert await adapter.send_stream_frame('preview', chat_id='chat', finalize=False)
        from tests.waiting import async_wait_until
        await async_wait_until(lambda: wire.events)
        assert any(row[2].get('msgtype') == 'stream' for row in wire.events)
        wire.events.clear()
        with strict_transport_scope():
            assert (await adapter.send('chat', 'sealed final')).success
        assert len(wire.events) == 1
        assert wire.events[0][2]['msgtype'] == 'markdown'
        for turn in adapter._stream_turns.values():
            adapter._cancel_keepalive(turn)


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', [None, 'lost'])
async def test_teams_cached_conversation_reference_media_uses_sdk_final_once(fault, tmp_path):
    from microsoft_teams.api import ConversationReference, ConversationAccount, Account
    path = tmp_path / 'proof.txt'
    await asyncio.to_thread(path.write_text, 'proof')
    endpoint = '/v3/conversations/chat/activities'
    async with peer({endpoint: fault} if fault else {}) as wire, teams(wire) as adapter:
        adapter._conv_refs['chat'] = ConversationReference(channel_id='msteams', service_url=wire.base,
            bot=Account(id='inert'), conversation=ConversationAccount(id='chat'))
        with strict_transport_scope():
            result = await adapter.send_document('chat', str(path))
        assert result.success is (fault is None), (result, wire.events)
        assert len(wire.events) == 1


@pytest.mark.asyncio
async def test_dingtalk_human_native_sdk_card_creates_delivers_streams_once():
    async with peer() as wire, dingtalk(wire) as adapter:
        wire.faults.clear()
        result = await adapter.send('chat', 'human final', reply_to='123')
        assert result.success, (result, wire.events)
        assert len(wire.events) == 3
        assert wire.events[0][1] == '/v1.0/card/instances'
        assert not any(row[1] == '/ding' for row in wire.events)


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', [None, 'lost', {'errcode': 40001}])
async def test_wecom_callback_strict_single_post_and_no_token_retry(fault, monkeypatch):
    from plugins.platforms.wecom import callback_adapter as module
    app = {'name': 'inert', 'corp_id': 'inert', 'agent_id': '1'}
    async with peer({'/callback-send': fault} if fault else {}) as wire, httpx.AsyncClient(trust_env=False) as client:
        monkeypatch.setattr(module, '_SEND_URL', wire.base + '/callback-send?access_token=')
        adapter = module.WecomCallbackAdapter(PlatformConfig(extra={'apps': [app]}))
        adapter._http_client = client
        adapter._access_tokens['inert'] = {'token': 'inert', 'expires_at': 9999999999}
        with strict_transport_scope():
            result = await adapter.send('inert:human', 'sealed final')
        assert result.success is (fault is None), (result, wire.events)
        assert len(wire.events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['feishu', 'google_chat', 'dingtalk', 'teams'])
async def test_strict_submit_does_not_follow_http_redirect(name):
    path = {'feishu': '/open-apis/im/v1/messages', 'google_chat': '/v1/spaces/peer/messages',
            'dingtalk': '/ding', 'teams': '/v3/conversations/chat/activities'}[name]
    async with peer({path: {'redirect': True}}) as wire, platform_peer(name, wire) as (adapter, chat):
        with strict_transport_scope():
            result = await adapter.send(chat, 'sealed final')
        assert not result.success
        assert len([row for row in wire.events if row[1] in {path, '/redirected'}]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', [{'code': 99992402}, 'lost'])
async def test_feishu_native_audio_thread_does_not_reroute(fault, tmp_path):
    path = tmp_path / 'proof.ogg'
    await asyncio.to_thread(path.write_bytes, b'OggSproof')
    async with peer({'/open-apis/im/v1/messages': fault}) as wire, feishu(wire) as adapter:
        with strict_transport_scope():
            result = await adapter.send_voice('chat', str(path), metadata={'thread_id': 'thread'})
        assert not result.success
        assert len([row for row in wire.events if '/messages' in row[1]]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('strict', [False, True])
async def test_google_human_retry_and_typing_patch_remain_available(strict):
    async with peer({'/v1/spaces/peer/messages': 503}) as wire, google_chat(wire) as adapter:
        adapter._typing_messages.clear()
        if strict:
            with strict_transport_scope():
                result = await adapter.send('spaces/peer', 'sealed final')
        else:
            result = await adapter.send('spaces/peer', 'human final')
        assert result.success is not strict
        assert len(wire.events) == (1 if strict else 2)


@pytest.mark.asyncio
async def test_google_human_typing_card_patched_in_place():
    async with peer() as wire, google_chat(wire) as adapter:
        assert (await adapter.send('spaces/peer', 'human final')).success
        assert [(row[0], row[1]) for row in wire.events] == [('PATCH', '/v1/spaces/peer/messages/typing')]


@pytest.mark.asyncio
@pytest.mark.parametrize('endpoint', ['aibot_upload_media_chunk', 'aibot_upload_media_finish'])
async def test_wecom_upload_chunk_or_finish_lost_ack_stops_before_send(endpoint, tmp_path):
    path = tmp_path / 'proof.txt'
    await asyncio.to_thread(path.write_text, 'proof')
    wire = SimpleNamespace(events=[], faults={endpoint: 'lost'})
    async with wecom(wire, passive=False) as adapter:
        with strict_transport_scope():
            result = await adapter.send_document('chat', str(path))
        assert not result.success
        assert len([row for row in wire.events if row[1] == endpoint]) == 1
        assert not any(row[1] == 'aibot_send_msg' for row in wire.events)


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', [None, 'lost', 401])
async def test_google_cached_per_user_media_credentials_single_attempt(fault, tmp_path):
    path = tmp_path / 'proof.txt'
    await asyncio.to_thread(path.write_text, 'proof')
    endpoint = '/v1/spaces/peer/messages'
    async with peer({endpoint: fault} if fault else {}) as wire, google_chat(wire) as adapter:
        adapter._last_sender_by_chat['spaces/peer'] = 'inert@example.invalid'
        adapter._user_chat_api_by_email['inert@example.invalid'] = adapter._chat_api
        adapter._user_creds_by_email['inert@example.invalid'] = adapter._credentials
        with strict_transport_scope():
            result = await adapter.send_document('spaces/peer', str(path))
        assert result.success is (fault is None), (result, wire.events)
        assert len([row for row in wire.events if row[1] == endpoint]) == 1
        assert len(wire.events) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['feishu', 'wecom', 'google_chat', 'teams'])
@pytest.mark.parametrize('kind', ['image_file', 'document', 'voice', 'video'])
async def test_human_native_media_success_uses_original_transport(name, kind, tmp_path):
    path = tmp_path / ('proof' + {'image_file': '.png', 'document': '.txt', 'voice': '.ogg' if name == 'feishu' else '.amr', 'video': '.mp4'}[kind])
    await asyncio.to_thread(path.write_bytes, b'\x89PNG\r\n\x1a\nproof' if kind == 'image_file' else b'#!AMR\nproof' if kind == 'voice' else b'proof')
    async with peer() as wire, platform_peer(name, wire) as (adapter, chat):
        if name == 'google_chat':
            adapter._user_credentials, adapter._user_chat_api = adapter._credentials, adapter._chat_api
        key = {'image_file': 'image_path', 'document': 'file_path', 'voice': 'audio_path', 'video': 'video_path'}[kind]
        result = await getattr(adapter, 'send_' + kind)(chat_id=chat, **{key: str(path)}, caption='human caption')
        assert result.success, (result, wire.events)
        assert wire.events
        if name == 'google_chat':
            assert wire.events[0][0] == 'PATCH'
