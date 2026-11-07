"""Mattermost strict batches stop at the first uncertain upload/post."""
import pytest

from gateway.strict_transport import strict_transport_scope, strict_call
from tests.gateway import test_strict_http_peers as peers


def images_in(tmp_path, count):
    paths = [tmp_path / f'{i}.png' for i in range(count)]
    for p in paths:
        p.write_bytes(b'image')
    return [(p.as_uri(), str(i)) for i, p in enumerate(paths)]


def writes(peer, suffix):
    return [r for r in peer.records if r.method == 'POST' and r.path.endswith(suffix)]


@pytest.mark.asyncio
@pytest.mark.parametrize('strict', [True, False], ids=['strict', 'human'])
async def test_mattermost_partial_batch_lost_ack(strict, tmp_path, monkeypatch):
    def fault(record, records):
        posts = [r for r in records if r.path.endswith('/posts')]
        return 'drop' if record.path.endswith('/posts') and len(posts) == 2 else None

    async with peers.http_peer(fault) as peer, peers.adapter_for('mattermost', peer, monkeypatch) as (adapter, chat):
        images = images_in(tmp_path, 11)
        if strict:
            with strict_transport_scope():
                result = await adapter.send_multiple_images(chat, images)
        else:
            result = await adapter.send_multiple_images(chat, images)
        assert result.success is (not strict)
        assert len(writes(peer, '/posts')) == (2 if strict else 8)
        assert len(writes(peer, '/files')) == (10 if strict else 16)


@pytest.mark.asyncio
async def test_mattermost_success_sends_all_native_chunks(tmp_path, monkeypatch):
    async with peers.http_peer() as peer, peers.adapter_for('mattermost', peer, monkeypatch) as (adapter, chat):
        result = await strict_call(adapter, 'send_multiple_images', chat_id=chat, images=images_in(tmp_path, 11))
        assert result.success
        posts = writes(peer, '/posts')
        assert [len(r.payload['file_ids']) for r in posts] == [5, 5, 1]
        assert len(writes(peer, '/files')) == 11
        assert all(b'image' in r.body for r in writes(peer, '/files'))


@pytest.mark.asyncio
@pytest.mark.parametrize('strict', [True, False], ids=['strict', 'human'])
async def test_mattermost_thread_fallback_compat(strict, monkeypatch):
    def fault(record, records):
        return 'reject' if record.payload and record.payload.get('root_id') else None

    async with peers.http_peer(fault) as peer, peers.adapter_for('mattermost', peer, monkeypatch) as (adapter, chat):
        adapter._reply_mode = 'thread'
        kwargs = {'reply_to': 'root', 'metadata': {'notify': True}}
        if strict:
            with strict_transport_scope():
                result = await adapter.send(chat, 'answer', **kwargs)
        else:
            result = await adapter.send(chat, 'answer', **kwargs)
        assert result.success is (not strict)
        assert len(writes(peer, '/posts')) == (1 if strict else 2)


@pytest.mark.asyncio
async def test_mattermost_upload_lost_ack_stops_batch(tmp_path, monkeypatch):
    def fault(record, records):
        return 'drop' if record.path.endswith('/files') else None

    async with peers.http_peer(fault) as peer, peers.adapter_for('mattermost', peer, monkeypatch) as (adapter, chat):
        with strict_transport_scope():
            result = await adapter.send_multiple_images(chat, images_in(tmp_path, 6))
        assert not result.success
        assert len(writes(peer, '/files')) == 1 and not writes(peer, '/posts')


@pytest.mark.asyncio
async def test_mattermost_missing_image_does_not_report_partial_success(tmp_path, monkeypatch):
    async with peers.http_peer() as peer, peers.adapter_for('mattermost', peer, monkeypatch) as (adapter, chat):
        images = images_in(tmp_path, 1) + [((tmp_path / 'missing.png').as_uri(), '')]
        with strict_transport_scope():
            result = await adapter.send_multiple_images(chat, images)
        assert not result.success and not writes(peer, '/posts')


@pytest.mark.asyncio
async def test_mattermost_remote_upload_failure_has_no_text_fallback(monkeypatch):
    monkeypatch.setattr('tools.url_safety.is_safe_url', lambda url: True)
    async with peers.http_peer(lambda r, _: 'reject' if r.path.endswith('/files') else None) as peer:
        async with peers.adapter_for('mattermost', peer, monkeypatch) as (adapter, chat):
            with strict_transport_scope():
                result = await adapter.send_image(chat, peer.url + '/image.png', caption='caption')
            assert not result.success and not writes(peer, '/posts')
            assert len(writes(peer, '/files')) == 1
