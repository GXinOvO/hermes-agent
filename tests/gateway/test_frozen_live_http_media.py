"""Media reference binding and native compatibility checks."""
import json

import pytest

from tests.gateway import frozen_live_http_peer as native


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', ['mattermost', 'feishu', 'google_chat'])
async def test_all_media_and_upload_references_are_preserved(platform, tmp_path):
    paths = [tmp_path / ('image.png' if i == 0 else f'file-{i}.bin') for i in range(3)]
    for i, path in enumerate(paths):
        path.write_bytes(f'original-media-{i}'.encode())
    async with native.peer() as wire, native.adapter_for(platform, wire) as (adapter, chat):
        state = native.state_for(platform, chat)
        calls = [('send_image_file' if i == 0 else 'send_document',
                  {('image_path' if i == 0 else 'file_path'): str(path), 'caption': f'caption-{i}'}) for i, path in enumerate(paths)]
        retained = [(str(path), False) for path in paths]
        plan = await native.prepare(platform, adapter, state, calls, retained)
        for path in paths:
            path.write_bytes(b'changed-media')
        result = await native.deliver(platform, plan)
        assert result['ok'] is True
        events = native.writes(wire)
        assert len(events) == 6
        for i in range(3):
            upload, post = events[2 * i:2 * i + 2]
            assert f'original-media-{i}'.encode() in upload.body
            assert b'changed-media' not in upload.body
            assert b'__HERMES_FROZEN_UPLOAD_REFERENCE__' not in post.body
            assert f'caption-{i}'.encode() in post.body
            if platform == 'mattermost':
                assert post.payload['file_ids'] == [f'file-{2 * i + 1}']
            elif platform == 'feishu':
                content = json.loads(post.payload['content'])
                assert content['zh_cn']['content'][-1][0].get('image_key', content['zh_cn']['content'][-1][0].get('file_key')) == (
                    f'image-{2 * i + 2}' if i == 0 else f'file-{2 * i + 2}')
            else:
                assert post.payload['attachment'] == [{'attachmentDataRef': {'resourceName': 'uploads/ref-A'}}]


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', ['mattermost', 'feishu', 'google_chat'])
@pytest.mark.parametrize('fault', ['lost', 'redirect', 401, 429])
async def test_media_upload_fault_prevents_post_and_later_media(platform, fault, tmp_path):
    path = tmp_path / 'proof.bin'
    path.write_bytes(b'proof')
    async with native.peer(lambda event, index: fault) as wire, native.adapter_for(platform, wire) as (adapter, chat):
        calls = [('send_document', {'file_path': str(path)}), ('send_document', {'file_path': str(path)})]
        plan = await native.prepare(platform, adapter, native.state_for(platform, chat), calls, [(str(path), False)])
        with pytest.raises(Exception):
            await native.deliver(platform, plan)
        assert len(native.writes(wire)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', ['mattermost', 'feishu', 'google_chat'])
async def test_malformed_ack_reference_cannot_inject_message_json(platform, tmp_path):
    path = tmp_path / 'proof.bin'
    path.write_bytes(b'proof')
    reference = 'quoted-"-reference\\tail'
    def ack(event, index):
        if platform == 'mattermost' and event.path.endswith('/files'):
            return {'file_infos': [{'id': reference}]}
        if platform == 'feishu' and event.path.endswith('/files'):
            return {'code': 0, 'data': {'file_key': reference}}
        if platform == 'google_chat' and event.path.startswith('/upload/'):
            return {'attachmentDataRef': {'resourceName': reference}}
    async with native.peer(ack) as wire, native.adapter_for(platform, wire) as (adapter, chat):
        plan = await native.prepare(platform, adapter, native.state_for(platform, chat),
            [('send_document', {'file_path': str(path)})], [(str(path), False)])
        assert (await native.deliver(platform, plan))['ok'] is True
        post = native.writes(wire)[1]
        if platform == 'mattermost':
            assert post.payload['file_ids'] == [reference]
        elif platform == 'feishu':
            assert json.loads(post.payload['content']) == {'file_key': reference}
        else:
            assert post.payload['attachment'][0]['attachmentDataRef']['resourceName'] == reference


@pytest.mark.asyncio
async def test_google_human_typing_patch_control():
    from tests.gateway import test_strict_enterprise_transport as legacy
    async with legacy.peer() as wire, legacy.google_chat(wire) as adapter:
        result = await adapter.send('spaces/peer', 'ordinary-human')
        assert result.success


@pytest.mark.asyncio
@pytest.mark.parametrize('method,kwargs', [
    ('send_image', {'image_url': 'https://invalid.example/image.png'}),
    ('send_video_file', {'video_path': '/missing/video.mp4'}),
    ('unknown_media', {'file_path': '/missing/document.bin'})])
async def test_weixin_unproven_media_refuses_entire_bundle_before_mutation(tmp_path, method, kwargs):
    path = tmp_path / 'proof.bin'
    path.write_bytes(b'proof')
    async with native.peer() as wire, native.adapter_for('weixin', wire) as (adapter, chat):
        with pytest.raises((ValueError, OSError)):
            await native.prepare('weixin', adapter, native.state_for('weixin', chat),
                [('send', {'content': 'must-not-send'}), (method, kwargs)], [(str(path), False)])
        assert native.writes(wire) == []
