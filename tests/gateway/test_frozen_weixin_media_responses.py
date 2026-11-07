"""Restricted response slots and cancellation of the native encrypted-media plan."""
import asyncio
import base64
from contextlib import asynccontextmanager
import json
from pathlib import Path
from types import SimpleNamespace

from aiohttp import web
import pytest

from plugins.platforms.weixin import adapter as wx
from plugins.platforms.weixin import frozen_delivery as delivery
from tests.gateway import frozen_live_http_peer as native
from tests.gateway import test_frozen_weixin_media_native as fixture


@asynccontextmanager
async def cdn_peer(mode='param', gate=None):
    wire = SimpleNamespace(events=[])

    async def handle(request):
        raw = await request.read()
        payload = json.loads(raw) if request.path.endswith(('/getuploadurl', '/sendmessage')) else None
        wire.events.append(SimpleNamespace(method=request.method, path=request.path, body=raw,
            payload=payload, headers=dict(request.headers), query=dict(request.query)))
        if gate is not None and len(wire.events) == gate.index:
            gate.accepted.set()
            await gate.release.wait()
        if request.path.endswith('/getuploadurl'):
            query = 'encrypted_query_param=opaque%2Fparam%3D&filekey=' + payload['filekey']
            url = wire.base + '/c2c/upload?' + query
            if mode == 'full':
                return web.json_response({'ret': 0, 'upload_full_url': url})
            if mode == 'empty-full':
                return web.json_response({'ret': 0, 'upload_full_url': '', 'upload_param': 'opaque/param='})
            if mode == 'bad-path':
                return web.json_response({'ret': 0, 'upload_full_url': wire.base + '/c2c/../admin?' + query})
            if mode == 'bad-key':
                return web.json_response({'ret': 0, 'upload_full_url': url.replace(payload['filekey'], 'wrong-key')})
            if mode == 'duplicate-query':
                return web.json_response({'ret': 0, 'upload_full_url': url + '&filekey=' + payload['filekey']})
            if mode == 'userinfo':
                return web.json_response({'ret': 0, 'upload_full_url': url.replace('http://', 'http://x:y@')})
            if mode == 'fragment':
                return web.json_response({'ret': 0, 'upload_full_url': url + '#ignored'})
            if mode == 'boolean-reference':
                return web.json_response({'ret': 0, 'upload_param': True})
            return web.json_response({'ret': 0, 'upload_param': 'opaque/param='})
        if request.path == '/c2c/upload':
            if mode == 'missing-cdn-reference':
                return web.Response()
            if mode == 'duplicate-cdn-reference':
                return web.Response(headers=[('x-encrypted-param', 'one'), ('x-encrypted-param', 'two')])
            if mode == 'lost-cdn-body':
                response = web.StreamResponse(headers={'x-encrypted-param': 'ref', 'Content-Length': '50'})
                await response.prepare(request)
                await response.write(b'accepted')
                request.transport.close()
                return response
            return web.Response(headers={'x-encrypted-param': 'quoted-"-reference\\tail'})
        if mode == 'missing-send-ack':
            return web.json_response({})
        if mode == 'boolean-send-ack':
            return web.json_response({'ret': False})
        return web.json_response({'ret': 0})

    app = web.Application()
    app.router.add_route('*', '/{tail:.*}', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    wire.base = f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'
    try:
        yield wire
    finally:
        print('WEIXIN_RESPONSE_WIRE ' + json.dumps({'mode': mode, 'paths': [e.path for e in wire.events],
            'sha256': [__import__('hashlib').sha256(e.body).hexdigest() for e in wire.events]}))
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['param', 'full', 'empty-full'])
async def test_native_allocation_response_branches_preserve_template_and_aes(mode, tmp_path):
    paths, calls, retained = fixture.bundle(tmp_path)
    async with cdn_peer(mode) as wire, native.adapter_for('weixin', wire) as (adapter, chat):
        adapter._cdn_base_url = wire.base + '/c2c'
        plan = await delivery.freeze(adapter, native.state_for('weixin', chat), calls[1:], retained)
        assert wire.events == []
        binding = plan['binding']
        assert (await delivery.send(plan))['ok'] is True
        allocation, upload, _caption, media = wire.events
        assert upload.query == {'encrypted_query_param': 'opaque/param=', 'filekey': allocation.payload['filekey']}
        assert 'Authorization' not in upload.headers
        key = bytes.fromhex(allocation.payload['aeskey'])
        assert wx._aes128_ecb_decrypt(upload.body, key) == paths[0].read_bytes()
        message = media.payload['msg']
        nested = message['item_list'][0]['file_item']
        assert nested['media']['encrypt_query_param'] == 'quoted-"-reference\\tail'
        assert base64.b64decode(nested['media']['aes_key']).decode() == key.hex()
        # _api_post's native insertion order is retained after slot resolution.
        assert media.body.startswith(b'{"msg":{"from_user_id":')
        assert int(media.headers['Content-Length']) == len(media.body)
        assert binding['prepared'][0]['response_slot']['path'] == '/c2c/upload'


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,count', [('bad-path', 1), ('bad-key', 1), ('duplicate-query', 1),
    ('userinfo', 1), ('fragment', 1), ('boolean-reference', 1), ('missing-cdn-reference', 2),
    ('duplicate-cdn-reference', 2), ('lost-cdn-body', 2), ('missing-send-ack', 3), ('boolean-send-ack', 3)])
async def test_malformed_response_dependencies_stop_at_own_stage(mode, count, tmp_path):
    _, calls, retained = fixture.bundle(tmp_path, ('first.bin', 'later.bin'))
    async with cdn_peer(mode) as wire, native.adapter_for('weixin', wire) as (adapter, chat):
        adapter._cdn_base_url = wire.base + '/c2c'
        plan = await delivery.freeze(adapter, native.state_for('weixin', chat), calls[1:] + calls[:1], retained)
        with pytest.raises(Exception):
            await delivery.send(plan)
        assert len(wire.events) == count


@pytest.mark.asyncio
@pytest.mark.parametrize('index', [1, 2, 3, 4])
async def test_cancellation_after_acceptance_stops_following_mutations(index, tmp_path):
    _, calls, retained = fixture.bundle(tmp_path, ('first.bin', 'later.bin'))
    gate = SimpleNamespace(index=index, accepted=asyncio.Event(), release=asyncio.Event())
    async with cdn_peer(gate=gate) as wire, native.adapter_for('weixin', wire) as (adapter, chat):
        adapter._cdn_base_url = wire.base + '/c2c'
        plan = await delivery.freeze(adapter, native.state_for('weixin', chat), calls[1:] + calls[:1], retained)
        task = asyncio.create_task(delivery.send(plan))
        await asyncio.wait_for(gate.accepted.wait(), timeout=5)
        task.cancel()
        gate.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(wire.events) == index
        with pytest.raises(RuntimeError, match='already attempted'):
            await delivery.send(plan)
        assert len(wire.events) == index


@pytest.mark.asyncio
@pytest.mark.parametrize('name,method,key,type_', [
    ('voice.silk', 'send_voice', 'audio_path', wx.ITEM_FILE),
    ('file.bin', 'send_document', 'file_path', wx.ITEM_FILE),
    ('image.png', 'send_image_file', 'image_path', wx.ITEM_IMAGE),
    ('video.mp4', 'send_video', 'video_path', wx.ITEM_VIDEO)])
async def test_voice_default_caption_and_human_native_wire_control(name, method, key, type_, tmp_path):
    paths, _, retained = fixture.bundle(tmp_path, (name,))
    options = {} if method == 'send_voice' else {'caption': 'caption-A'}
    calls = [(method, {key: str(paths[0]), **options})]
    async with cdn_peer() as wire, native.adapter_for('weixin', wire) as (adapter, chat):
        adapter._cdn_base_url = wire.base + '/c2c'
        state = native.state_for('weixin', chat)
        plan = await delivery.freeze(adapter, state, calls, retained)
        assert (await delivery.send(plan))['ok'] is True
        frozen_events = wire.events[:]
        assert (await getattr(adapter, method)(chat, str(paths[0]), **options)).success is True
        human_events = wire.events[len(frozen_events):]
        assert len(frozen_events) == len(human_events) == 4
        for events in [frozen_events, human_events]:
            allocation, upload, caption, media = events
            key = bytes.fromhex(allocation.payload['aeskey'])
            assert wx._aes128_ecb_decrypt(upload.body, key) == paths[0].read_bytes()
            assert media.payload['msg']['item_list'][0]['type'] == type_
            assert caption.payload['msg']['item_list'][0]['text_item']['text']
        def normalized(event):
            value = json.loads(event.body)
            if 'msg' in value:
                value['msg'].pop('client_id')
                for item in value['msg']['item_list']:
                    if item['type'] != wx.ITEM_TEXT:
                        nested = next(v for k, v in item.items() if k.endswith('_item'))
                        nested['media']['aes_key'] = '<random-key>'
            else:
                value['aeskey'] = '<random-key>'
                value['filekey'] = '<random-filekey>'
            return value
        assert normalized(frozen_events[0]) == normalized(human_events[0])
        assert normalized(frozen_events[2]) == normalized(human_events[2])
        assert normalized(frozen_events[3]) == normalized(human_events[3])


@pytest.mark.asyncio
async def test_fresh_process_rebuild_and_independent_media_plans(tmp_path):
    import os
    import sys
    _paths, calls, retained = fixture.bundle(tmp_path)
    async with cdn_peer() as wire, native.adapter_for('weixin', wire) as (adapter, chat):
        adapter._cdn_base_url = wire.base + '/c2c'
        state = native.state_for('weixin', chat)
        state.execution_id = 'execution-A'
        first, second = await asyncio.gather(delivery.freeze(adapter, state, calls, retained),
            delivery.freeze(adapter, state, calls, retained))
        assert first['binding']['crypto_context'] != second['binding']['crypto_context']
        value = {'base': wire.base, 'cdn': adapter._cdn_base_url, 'state': vars(state),
            'calls': calls, 'retained': retained, 'binding': first['binding']}
        # The native test runner already supplies isolated HOME/HERMES_HOME/TMPDIR.
        env = {key: os.environ[key] for key in ['PATH', 'HOME', 'HERMES_HOME', 'TMPDIR']}
        proc = await asyncio.create_subprocess_exec(sys.executable, '-m',
            'tests.gateway.test_frozen_weixin_media_rebuild_support',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env)
        output, error = await asyncio.wait_for(proc.communicate(json.dumps(value).encode()), timeout=20)
        assert proc.returncode == 0, error.decode()
        assert json.loads(output)['binding'] == first['binding']
        assert wire.events == []
        altered = first['binding']
        altered['prepared'].clear()
        assert first['binding'] != altered
        outcomes = await asyncio.gather(delivery.send(first), delivery.send(first), delivery.send(second),
            return_exceptions=True)
        assert sum(isinstance(x, dict) and x.get('ok') is True for x in outcomes) == 2
        assert sum(isinstance(x, RuntimeError) for x in outcomes) == 1
        allocations = [e for e in wire.events if e.path.endswith('/getuploadurl')]
        assert len(allocations) == 2
        assert allocations[0].payload['aeskey'] != allocations[1].payload['aeskey']
        assert len(wire.events) == 10  # each: body, allocate, upload, caption, media
