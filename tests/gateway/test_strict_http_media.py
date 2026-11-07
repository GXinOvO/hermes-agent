"""Media method qualification uses real HTTP posts, uploads, and lost ACKs."""
from pathlib import Path

import pytest

from gateway.strict_transport import strict_call, strict_transport_scope
from tests.gateway import test_strict_http_peers as peers

LOCAL_METHODS = [
    ('bluebubbles', 'send_image_file', 'image_path'), ('bluebubbles', 'send_voice', 'audio_path'),
    ('bluebubbles', 'send_video', 'video_path'), ('bluebubbles', 'send_document', 'file_path'),
    ('mattermost', 'send_image_file', 'image_path'), ('mattermost', 'send_voice', 'audio_path'),
    ('mattermost', 'send_video', 'video_path'), ('mattermost', 'send_document', 'file_path'),
    ('whatsapp_cloud', 'send_image_file', 'image_path'), ('whatsapp_cloud', 'send_voice', 'audio_path'),
    ('whatsapp_cloud', 'send_video', 'video_path'), ('whatsapp_cloud', 'send_document', 'file_path'),
    ('signal', 'send_image_file', 'image_path'), ('signal', 'send_voice', 'audio_path'),
    ('signal', 'send_video', 'video_path'), ('signal', 'send_document', 'file_path'),
]


def sent_messages(peer, name):
    if name == 'signal':
        return [r for r in peer.records if r.payload and r.payload.get('method') == 'send']
    if name in ('mattermost', 'whatsapp_cloud'):
        return [r for r in peer.records if r.path.endswith('/posts') or r.path.endswith('/messages')]
    return [r for r in peer.records if r.method == 'POST']


@pytest.mark.asyncio
@pytest.mark.parametrize('name,method,key', LOCAL_METHODS)
@pytest.mark.parametrize('fault', [None, 'drop', 'missing'], ids=['success', 'lost-ack', 'missing-ack'])
async def test_native_local_media(name, method, key, fault, tmp_path, monkeypatch):
    path = tmp_path / 'media.png'
    path.write_bytes(b'local-media')
    def fail(record, records):
        target = (record.payload or {}).get('method') == 'send' if name == 'signal' else record.method == 'POST'
        return fault if target else None

    async with peers.http_peer(fail) as peer, peers.adapter_for(name, peer, monkeypatch) as (adapter, chat):
        result = await strict_call(adapter, method, chat_id=chat, **{key: str(path)})
        assert result.success is (fault is None)
        messages = sent_messages(peer, name)
        if name in ('mattermost', 'whatsapp_cloud'):
            assert len(messages) == (1 if fault is None else 0)
            uploads = [r for r in peer.records if r.path.endswith('/files') or r.path.endswith('/media')]
            assert len(uploads) == 1 and b'local-media' in uploads[0].body
        else:
            assert len(messages) == 1
            if name == 'signal':
                attachment = messages[0].payload['params']['attachments'][0]
                assert Path(attachment).read_bytes() == b'local-media'
            else:
                assert b'local-media' in messages[0].body


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['bluebubbles', 'mattermost', 'whatsapp_cloud', 'signal'])
@pytest.mark.parametrize('fault', [None, 'drop', 'missing'])
async def test_native_url_image(name, fault, monkeypatch):
    import httpx
    monkeypatch.setattr('tools.url_safety.is_safe_url', lambda url: True)
    monkeypatch.setattr('tools.url_safety.create_ssrf_safe_async_client',
                        lambda **kwargs: httpx.AsyncClient(trust_env=False, **kwargs))
    def fail(record, records):
        if name == 'signal':
            return fault if (record.payload or {}).get('method') == 'send' else None
        return fault if record.method == 'POST' else None

    async with peers.http_peer(fail) as peer, peers.adapter_for(name, peer, monkeypatch) as (adapter, chat):
        result = await strict_call(adapter, 'send_image', chat_id=chat, image_url=peer.url + '/image.png', caption='caption')
        assert result.success is (fault is None)
        sent = sent_messages(peer, name)
        expected = 0 if name == 'mattermost' and fault else 2 if name == 'bluebubbles' and fault is None else 1
        assert len(sent) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['drop', 'missing'])
async def test_cloud_message_ack_failure_stops_media_plan(fault, tmp_path, monkeypatch):
    from gateway.background_messaging_results import deliver_plan
    local = tmp_path / 'media.png'
    local.write_bytes(b'local-media')
    async with peers.http_peer(lambda r, _: fault if r.path.endswith('/messages') else None) as peer:
        async with peers.adapter_for('whatsapp_cloud', peer, monkeypatch) as (adapter, chat):
            plan = [('send_image_file', {'chat_id': chat, 'image_path': str(local)})] * 3
            with pytest.raises(RuntimeError, match='acknowledgement'):
                await deliver_plan(adapter, plan)
            assert len([r for r in peer.records if r.path.endswith('/media')]) == 1
            assert len([r for r in peer.records if r.path.endswith('/messages')]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('strict', [True, False], ids=['strict', 'human'])
async def test_cloud_voice_caption_on_missing_file(strict, monkeypatch):
    async with peers.http_peer() as peer, peers.adapter_for('whatsapp_cloud', peer, monkeypatch) as (adapter, chat):
        if strict:
            with strict_transport_scope():
                result = await adapter.send_voice(chat, '/missing/audio.ogg', caption='caption')
        else:
            result = await adapter.send_voice(chat, '/missing/audio.ogg', caption='caption')
        assert not result.success
        assert not sent_messages(peer, 'whatsapp_cloud')


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['mattermost', 'whatsapp_cloud', 'bluebubbles', 'signal'])
async def test_native_image_explicit_human_retry(name, tmp_path, monkeypatch):
    local = tmp_path / 'media.png'
    local.write_bytes(b'local-media')
    def fail(record, records):
        if name == 'signal':
            sent = [r for r in records if (r.payload or {}).get('method') == 'send']
        else:
            sent = [r for r in records if r.method == 'POST']
        return 'drop' if record in sent and len(sent) == 1 else None

    async with peers.http_peer(fail) as peer, peers.adapter_for(name, peer, monkeypatch) as (adapter, chat):
        if name == 'mattermost':
            import aiohttp
            with pytest.raises(aiohttp.ClientError):
                await adapter.send_image_file(chat, str(local))
            first = None
        else:
            first = await adapter.send_image_file(chat, str(local))
        second = await adapter.send_image_file(chat, str(local))
        assert (first is None or not first.success) and second.success
