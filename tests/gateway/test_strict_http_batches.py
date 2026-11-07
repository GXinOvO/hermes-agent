"""Inherited image loops must not continue after an uncertain strict send."""
import httpx
import pytest

from gateway.strict_transport import strict_call, strict_transport_scope
from tests.gateway import test_strict_http_peers as peers


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['bluebubbles', 'whatsapp_cloud'])
@pytest.mark.parametrize('fault', [None, 'drop'], ids=['all-ack', 'lost-ack'])
async def test_strict_native_image_loop_preserves_all_or_stops(name, fault, tmp_path, monkeypatch):
    from gateway.strict_transport import supports_strict_transport
    paths = [tmp_path / f'{i}.png' for i in range(3)]
    for i, path in enumerate(paths):
        path.write_bytes(f'media-{i}'.encode())
    def fail(record, records):
        sent = [r for r in records if r.method == 'POST' and (name == 'bluebubbles' or r.path.endswith('/messages'))]
        return fault if record in sent and len(sent) == 2 else None

    async with peers.http_peer(fail) as peer, peers.adapter_for(name, peer, monkeypatch) as (adapter, chat):
        assert supports_strict_transport(adapter, 'send_multiple_images')
        result = await strict_call(adapter, 'send_multiple_images', chat_id=chat, images=[(p.as_uri(), '') for p in paths])
        assert result.success is (fault is None)
        sent = [r for r in peer.records if r.method == 'POST' and (name == 'bluebubbles' or r.path.endswith('/messages'))]
        assert len(sent) == (3 if fault is None else 2)


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['bluebubbles', 'whatsapp_cloud'])
async def test_human_image_loop_keeps_original_continue_policy(name, tmp_path, monkeypatch):
    path = tmp_path / 'image.png'
    path.write_bytes(b'image')
    def fail(record, records):
        sent = [r for r in records if r.method == 'POST' and (name == 'bluebubbles' or r.path.endswith('/messages'))]
        return 'drop' if record in sent and len(sent) == 2 else None

    async with peers.http_peer(fail) as peer, peers.adapter_for(name, peer, monkeypatch) as (adapter, chat):
        result = await adapter.send_multiple_images(chat, [(path.as_uri(), '')] * 3)
        assert result.success
        assert len([r for r in peer.records if r.method == 'POST' and (name == 'bluebubbles' or r.path.endswith('/messages'))]) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['bluebubbles', 'signal', 'mattermost'])
async def test_strict_url_download_failure_is_one_attempt_no_send(name, monkeypatch):
    monkeypatch.setattr('tools.url_safety.is_safe_url', lambda url: True)
    monkeypatch.setattr('tools.url_safety.create_ssrf_safe_async_client',
                        lambda **kwargs: httpx.AsyncClient(trust_env=False, **kwargs))
    async with peers.http_peer(lambda r, _: 'drop' if r.method == 'GET' else None) as peer:
        async with peers.adapter_for(name, peer, monkeypatch) as (adapter, chat):
            result = await strict_call(adapter, 'send_image', chat_id=chat, image_url=peer.url + '/image.png')
            assert not result.success
            assert len([r for r in peer.records if r.method == 'GET']) == 1
            assert not any(r.method == 'POST' and (r.payload or {}).get('method', 'send') == 'send' for r in peer.records)


@pytest.mark.asyncio
async def test_bluebubbles_caption_lost_ack_stops_following_images(tmp_path, monkeypatch):
    path = tmp_path / 'image.png'
    path.write_bytes(b'image')
    async with peers.http_peer(lambda r, _: 'drop' if r.path.endswith('/text') else None) as peer:
        async with peers.adapter_for('bluebubbles', peer, monkeypatch) as (adapter, chat):
            with strict_transport_scope():
                result = await adapter.send_multiple_images(chat, [(path.as_uri(), 'caption')] * 3)
            assert not result.success
            assert len([r for r in peer.records if r.path.endswith('/attachment')]) == 1
            assert len([r for r in peer.records if r.path.endswith('/text')]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('success', [True, False], ids=['all-ack', 'lost-ack'])
async def test_email_local_image_is_real_attachment(success, tmp_path):
    path = tmp_path / 'image.png'
    path.write_bytes(b'local-image')
    async with peers.smtp_peer(drop_first=not success) as peer:
        result = await strict_call(peers.email_for(peer), 'send_image_file',
                                   chat_id='receiver@inert.invalid', image_path=str(path))
        assert result.success is success and len(peer.messages) == 1
        from email import policy
        from email.parser import BytesParser
        msg = BytesParser(policy=policy.default).parsebytes(peer.messages[0])
        assert [p.get_payload(decode=True) for p in msg.iter_attachments()] == [b'local-image']
