"""Yuanbao: real HTTP/COS upload followed by real WS dispatch/ACK."""
import asyncio
import json
import os
from collections import Counter
from contextlib import asynccontextmanager
from functools import partial
from types import SimpleNamespace

import httpx
import pytest
from aiohttp import web
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from gateway.config import PlatformConfig
from gateway.strict_transport import strict_call, strict_transport_scope


@asynccontextmanager
async def yuanbao_peer(monkeypatch, fault=None, upload_fault=None):
    from plugins.platforms.yuanbao import adapter as module
    from plugins.platforms.yuanbao import yuanbao_media as media
    from plugins.platforms.yuanbao import yuanbao_proto as proto
    uploads, frames = [], []

    async def upload(request):
        body = await request.read()
        if request.path == media.UPLOAD_INFO_PATH:
            uploads.append(('POST-credentials', body))
            return web.json_response({'code': 0, 'data': {'encryptTmpSecretId': 'inert', 'encryptTmpSecretKey': 'inert',
                'encryptToken': 'inert', 'bucketName': 'inert', 'region': 'inert', 'location': 'image.png'}})
        if request.method == 'GET':
            return web.Response(body=b'image', content_type='image/png')
        uploads.append((request.method, body))
        if upload_fault == 'drop':
            request.transport.close()
            return web.Response()
        return web.Response(status=200)

    app = web.Application()
    app.router.add_route('*', '/{path:.*}', upload)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    http_url = f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'
    real_client = httpx.AsyncClient

    class LoopbackClient(real_client):
        async def request(self, method, url, *args, **kwargs):
            # Only endpoint routing changes. Production signing and HTTP PUT still run.
            if '.myqcloud.com/' in str(url):
                url = http_url + '/cos'
            return await super().request(method, url, *args, **kwargs)

    monkeypatch.setattr(media, 'httpx', SimpleNamespace(AsyncClient=LoopbackClient))

    async def token():
        return {'token': 'inert'}

    async def ws_peer(socket):
        async for raw in socket:
            msg = proto.decode_conn_msg(raw)
            frames.append(msg)
            if fault == 'missing-first' and len(frames) == 1:
                continue
            if fault == 'drop':
                await socket.close(code=1011, reason='lost ACK')
                return
            head = msg['head']
            response = proto.encode_conn_msg_full(proto.CMD_TYPE['Response'], head['cmd'], head['seq_no'],
                head['msg_id'], head['module'], b'')
            if fault == 'reject':
                response = proto._m(1, proto._encode_head(proto.CMD_TYPE['Response'], head['cmd'], head['seq_no'],
                    head['msg_id'], head['module'], status=500))
            await socket.send(response)

    adapter = module.YuanbaoAdapter(PlatformConfig(extra={}))
    adapter._get_cached_token = token
    adapter._api_domain = http_url
    receive_task = None
    try:
        async with serve(ws_peer, '127.0.0.1', 0) as server:
            async with connect(f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}') as socket:
                adapter._connection._ws = socket
                adapter._connection.send_biz_request = partial(adapter._connection.send_biz_request, timeout=0.05)
                async def receive():
                    try:
                        async for raw in socket:
                            await adapter._connection._handle_frame(raw)
                    except Exception as exc:
                        from websockets.exceptions import ConnectionClosed
                        if not isinstance(exc, ConnectionClosed):
                            raise
                receive_task = asyncio.create_task(receive())
                adapter._test_http_url = http_url
                yield adapter, uploads, frames
    finally:
        print('STRICT_HTTP_TRANSPORT ' + json.dumps({'test': os.environ.get('PYTEST_CURRENT_TEST'), 'kind': 'yuanbao',
            'http': dict(Counter(method for method, _ in uploads)),
            'ws': dict(Counter(f['head']['cmd'] for f in frames))}, sort_keys=True))
        if receive_task:
            receive_task.cancel()
            await asyncio.gather(receive_task, return_exceptions=True)
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize('method,key', [('send_image_file', 'image_path'), ('send_document', 'file_path')])
@pytest.mark.parametrize('fault', [None, 'drop', 'reject'], ids=['success', 'lost-ack', 'rejected'])
async def test_yuanbao_native_media_http_and_ws(method, key, fault, tmp_path, monkeypatch):
    path = tmp_path / 'image.png'
    path.write_bytes(b'local-media')
    async with yuanbao_peer(monkeypatch, fault=fault) as (adapter, uploads, frames):
        result = await strict_call(adapter, method, chat_id='direct:inert', **{key: str(path)})
        assert result.success is (fault is None)
        assert [kind for kind, _ in uploads] == ['POST-credentials', 'PUT']
        assert uploads[-1][1] == b'local-media'
        assert len(frames) == 1


@pytest.mark.asyncio
async def test_yuanbao_media_upload_lost_ack_has_no_message(tmp_path, monkeypatch):
    path = tmp_path / 'image.png'
    path.write_bytes(b'local-media')
    async with yuanbao_peer(monkeypatch, upload_fault='drop') as (adapter, uploads, frames):
        with strict_transport_scope():
            result = await adapter.send_image_file('direct:inert', str(path))
        assert not result.success and len(uploads) == 2 and frames == []


@pytest.mark.asyncio
async def test_yuanbao_media_plan_stops_at_unknown_ack(tmp_path, monkeypatch):
    from gateway.background_messaging_results import deliver_plan
    path = tmp_path / 'image.png'
    path.write_bytes(b'local-media')
    async with yuanbao_peer(monkeypatch, fault='drop') as (adapter, uploads, frames):
        with pytest.raises(RuntimeError, match='acknowledgement'):
            await deliver_plan(adapter, [('send_image_file', {'chat_id': 'direct:inert', 'image_path': str(path)})] * 3)
        assert len(uploads) == 2 and len(frames) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', [None, 'drop'])
async def test_yuanbao_url_image(fault, monkeypatch):
    monkeypatch.setattr('tools.url_safety.is_safe_url', lambda url: True)
    monkeypatch.setattr('tools.url_safety.create_ssrf_safe_async_client',
                        lambda **kwargs: httpx.AsyncClient(trust_env=False, **kwargs))
    async with yuanbao_peer(monkeypatch, fault=fault) as (adapter, uploads, frames):
        result = await strict_call(adapter, 'send_image', chat_id='direct:inert',
                                   image_url=adapter._test_http_url + '/image.png')
        assert result.success is (fault is None)
        assert len(uploads) == 3 and len(frames) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', [None, 'missing-first', 'reject'])
async def test_yuanbao_text_chunks(fault, monkeypatch):
    async with yuanbao_peer(monkeypatch, fault=fault) as (adapter, uploads, frames):
        adapter.MAX_TEXT_CHUNK = 64
        result = await strict_call(adapter, 'send', chat_id='direct:inert',
                                   content='\n\n'.join(c * 40 for c in 'abc'))
        assert result.success is (fault is None)
        messages = [f for f in frames if f['head']['cmd'] in ('send_c2c_message', 'send_group_message')]
        assert len(messages) == (3 if fault is None else 1)
        assert uploads == []


@pytest.mark.asyncio
async def test_yuanbao_human_text_keeps_automatic_retry(monkeypatch):
    async with yuanbao_peer(monkeypatch, fault='missing-first') as (adapter, uploads, frames):
        result = await adapter.send('direct:inert', 'answer')
        assert result.success and len(frames) == 2 and not uploads


@pytest.mark.asyncio
async def test_yuanbao_explicit_human_media_retry(tmp_path, monkeypatch):
    path = tmp_path / 'image.png'
    path.write_bytes(b'local-media')
    async with yuanbao_peer(monkeypatch, fault='missing-first') as (adapter, uploads, frames):
        first = await adapter.send_image_file('direct:inert', str(path))
        second = await adapter.send_image_file('direct:inert', str(path))
        assert not first.success and second.success
        assert len(uploads) == 4 and len(frames) == 2
