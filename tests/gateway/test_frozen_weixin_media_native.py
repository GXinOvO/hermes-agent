"""Frozen local iLink media, exercised against inert real HTTP peers."""
import asyncio
import base64
import hashlib
import json

import pytest
from aiohttp import web
from contextlib import asynccontextmanager

from plugins.platforms.weixin import adapter as wx
from plugins.platforms.weixin import frozen_delivery as delivery
from tests.gateway import frozen_live_http_peer as native


@asynccontextmanager
async def media_peer(fault=None, upload_param=False):
    async with native.peer(fault) as wire:
        # The existing peer owns wire capture and fault injection. Its explicit
        # direct-CDN endpoint also qualifies the unchanged historical oracle.
        wire.upload_param = upload_param
        yield wire


def bundle(tmp_path, names=('file.bin',)):
    paths = [tmp_path / name for name in names]
    for index, path in enumerate(paths):
        path.write_bytes(b'original-A\x00\xff' + bytes([index]))
    methods = {'png': ('send_image_file', 'image_path'), 'mp4': ('send_video', 'video_path'),
               'silk': ('send_voice', 'audio_path')}
    calls = [('send', {'content': 'body-A', 'metadata': {'job_id': 'job-A'}})]
    for path in paths:
        method, key = methods.get(path.suffix[1:], ('send_document', 'file_path'))
        calls.append((method, {key: str(path), 'caption': 'caption-A'}))
    return paths, calls, [(str(p), p.suffix == '.silk') for p in paths]


def messages(wire):
    return [e for e in native.writes(wire) if e.path.endswith('/sendmessage')]


@pytest.mark.asyncio
async def test_native_media_crypto_and_fresh_binding(tmp_path):
    paths, calls, retained = bundle(tmp_path, ('file.bin', 'image.png', 'video.mp4', 'voice.silk'))
    originals = [p.read_bytes() for p in paths]
    async with media_peer() as wire, native.adapter_for('weixin', wire) as (adapter, chat):
        state = native.state_for('weixin', chat)
        state.execution_id = 'execution-A'
        plan = await delivery.freeze(adapter, state, calls, retained)
        assert wire.events == []
        binding = plan['binding']
        async with native.adapter_for('weixin', wire) as (fresh, _):
            recovered = await delivery.freeze(fresh, state, calls, retained, saved_binding=binding)
            assert recovered['binding'] == binding
        for path in paths:
            path.write_bytes(b'changed-B')
        adapter._base_url = adapter._cdn_base_url = 'http://127.0.0.1:1/changed'
        adapter._token, adapter._account_id = 'changed-token', 'changed-account'
        adapter._token_store._cache.clear()
        adapter._split_text = lambda _: ['changed-chunk']
        state.chat_id = 'changed-target'
        calls[0][1]['content'] = 'changed-body'
        result = await delivery.send(recovered)
        assert result['ok'] is True and result['message_id']
        allocations = [e for e in wire.events if e.path.endswith('/getuploadurl')]
        uploads = [e for e in wire.events if e.path == '/cdn-upload']
        assert len(allocations) == len(uploads) == 4
        assert len(messages(wire)) == 9  # body; then caption and media per file
        expected_types = [wx.ITEM_FILE, wx.ITEM_IMAGE, wx.ITEM_VIDEO, wx.ITEM_FILE]
        for index, (allocation, upload, original) in enumerate(zip(allocations, uploads, originals)):
            key = bytes.fromhex(allocation.payload['aeskey'])
            assert len(key) == 16
            assert wx._aes128_ecb_decrypt(upload.body, key) == original
            assert upload.body == wx._aes128_ecb_encrypt(original, key)
            assert allocation.payload['rawfilemd5'] == hashlib.md5(original).hexdigest()
            assert allocation.payload['filesize'] == len(upload.body)
            assert 'Authorization' not in upload.headers
            item = messages(wire)[2 + index * 2].payload['msg']['item_list'][0]
            assert item['type'] == expected_types[index]
            nested = next(value for key_, value in item.items() if key_.endswith('_item'))
            assert base64.b64decode(nested['media']['aes_key']).decode('ascii') == key.hex()
        for event in messages(wire):
            assert event.payload['msg']['context_token'] == 'synthetic-context-A'
            assert event.payload['msg']['to_user_id'] == chat
            assert event.headers['Authorization'] == 'Bearer synthetic-weixin-A'
        public = json.dumps(binding)
        assert 'synthetic-context-A' not in public and 'synthetic-weixin-A' not in public
        for allocation in allocations:
            assert allocation.payload['aeskey'] not in public
            assert allocation.payload['filekey'] not in public
        for request in binding['prepared']:
            if request.get('body_b64'):
                decoded = base64.b64decode(request['body_b64'])
                assert b'synthetic-context-A' not in decoded
                for allocation in allocations:
                    assert allocation.payload['aeskey'].encode() not in decoded
        print('WEIXIN_CRYPTO_PROOF ' + json.dumps({'media': 4, 'native_aes_decrypt': True,
            'allocation_sha256': [hashlib.sha256(e.body).hexdigest() for e in allocations],
            'ciphertext_sha256': [hashlib.sha256(e.body).hexdigest() for e in uploads]}))


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['envelope', 'target', 'execution', 'token', 'context', 'media', 'base', 'home'])
async def test_saved_binding_rejects_tamper_and_wrong_scope(change, tmp_path, monkeypatch):
    paths, calls, retained = bundle(tmp_path)
    async with media_peer() as wire, native.adapter_for('weixin', wire) as (adapter, chat):
        state = native.state_for('weixin', chat)
        state.execution_id = 'execution-A'
        plan = await delivery.freeze(adapter, state, calls, retained)
        saved = plan['binding']
        if change == 'envelope':
            encoded = saved['crypto_context']['ciphertext']
            saved['crypto_context']['ciphertext'] = ('A' if encoded[0] != 'A' else 'B') + encoded[1:]
            # A recomputed public digest cannot authenticate a modified envelope.
            from plugins.platforms.frozen_delivery import digest, encode
            saved.pop('transport_sha256')
            saved['transport_sha256'] = digest(encode(saved))
        else:
            changes = {
                'target': lambda: setattr(state, 'chat_id', 'peer-B'),
                'execution': lambda: setattr(state, 'execution_id', 'execution-B'),
                'token': lambda: setattr(adapter, '_token', 'other-inert-token'),
                'context': lambda: adapter._token_store._cache.__setitem__(
                    adapter._token_store._key(adapter._account_id, chat), 'context-B'),
                'media': lambda: paths[0].write_bytes(b'changed-input'),
                'base': lambda: setattr(adapter, '_cdn_base_url', adapter._cdn_base_url + '/other'),
                'home': lambda: monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'other-home')),
            }
            changes[change]()
        with pytest.raises(ValueError):
            await delivery.freeze(adapter, state, calls, retained, saved_binding=saved)
        assert wire.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['allocate', 'upload', 'caption', 'media', 'text'])
@pytest.mark.parametrize('fault', [503, 'redirect', 401, 429, 'lost'])
async def test_each_mutation_fault_stops_current_and_later_publication(stage, fault, tmp_path):
    _, calls, retained = bundle(tmp_path, ('first.bin', 'later.bin'))
    # Media first makes every dependent stage reachable independently of text.
    calls = calls[1:] + calls[:1]
    target_index = {'allocate': 1, 'upload': 2, 'caption': 3, 'media': 4, 'text': 9}[stage]
    async with media_peer(lambda event, index: fault if index == target_index else None) as wire:
        async with native.adapter_for('weixin', wire) as (adapter, chat):
            plan = await delivery.freeze(adapter, native.state_for('weixin', chat), calls, retained)
            with pytest.raises(Exception):
                await delivery.send(plan)
            assert len(native.writes(wire)) == target_index
            with pytest.raises(RuntimeError, match='already attempted'):
                await delivery.send(plan)
            assert len(native.writes(wire)) == target_index


@pytest.mark.asyncio
@pytest.mark.parametrize('ack', [{}, {'ret': 0}, {'ret': False, 'upload_param': 'x'},
    {'ret': 0, 'upload_full_url': 'http://127.0.0.1:1/cdn-upload'},
    {'ret': 0, 'upload_full_url': 'file:///etc/passwd'},
    {'ret': 0, 'upload_full_url': 'https://novac2c.cdn.weixin.qq.com.evil.invalid/c2c/upload'},
    {'ret': 0, 'upload_param': ''}])
async def test_bad_upload_allocation_has_zero_followup(ack, tmp_path):
    _, calls, retained = bundle(tmp_path)
    async with media_peer(lambda event, index: ack) as wire, native.adapter_for('weixin', wire) as (adapter, chat):
        plan = await delivery.freeze(adapter, native.state_for('weixin', chat), calls[1:] + calls[:1], retained)
        with pytest.raises(Exception):
            await delivery.send(plan)
        assert len(native.writes(wire)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', ['missing', 'remote', 'silk-native', 'inline'])
async def test_whole_bundle_preflight_before_first_mutation(bad, tmp_path):
    paths, calls, retained = bundle(tmp_path)
    if bad == 'missing':
        calls.append(('send_document', {'file_path': str(tmp_path / 'missing.bin')}))
        retained.append((str(tmp_path / 'missing.bin'), False))
    elif bad == 'remote':
        calls.append(('send_image', {'image_url': 'https://example.invalid/image.png', 'caption': ''}))
    elif bad == 'silk-native':
        silk = tmp_path / 'native.silk'
        silk.write_bytes(b'silk')
        calls.append(('send_document', {'file_path': str(silk)}))
        retained.append((str(silk), False))
    else:
        calls.append(('send', {'content': 'MEDIA:' + str(paths[0])}))
    async with media_peer() as wire, native.adapter_for('weixin', wire) as (adapter, chat):
        with pytest.raises((ValueError, OSError)):
            await delivery.freeze(adapter, native.state_for('weixin', chat), calls, retained)
        assert wire.events == []
