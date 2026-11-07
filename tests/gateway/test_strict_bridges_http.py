"""Actual HTTP/SDK receipts for strict long-tail bridge sends; no remote traffic."""
from contextlib import asynccontextmanager
import time
import hashlib
import json
from types import SimpleNamespace

from aiohttp import web
import httpx
import pytest

from gateway.config import PlatformConfig
from gateway.strict_transport import strict_call


@asynccontextmanager
async def http_peer(fault=None, fail_at=1):
    wire = SimpleNamespace(events=[], base='')

    async def receive(request):
        data = await request.read()
        wire.events.append((request.method, request.path, data))
        if len(wire.events) == fail_at:
            if fault == 'lost':
                request.transport.close()
                return web.Response()
            if fault == 'redirect':
                return web.Response(status=307, headers={'Location':wire.base + '/redirected'})
            if fault == '503':
                return web.json_response({'errcode':'M_UNKNOWN', 'error':'lost upstream'}, status=503)
        if request.path.endswith('/upload_prepare'):
            return web.json_response({'upload_id':'local-upload', 'block_size':1048576,
                'parts':[{'part_index':1, 'presigned_url':wire.base + '/cos/1'}]})
        if '/upload' in request.path or request.path.endswith('/files'):
            return web.json_response({'content_uri':'mxc://local/media', 'file_info':'media-ticket'})
        return web.json_response({'id':'ack', 'event_id':'$ack'})

    app = web.Application()
    app.router.add_route('*', '/{path:.*}', receive)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    wire.base = 'http://127.0.0.1:' + str(site._server.sockets[0].getsockname()[1])
    try:
        yield wire
    finally:
        print('WIRE_HTTP ' + json.dumps([{'method':method, 'path':path, 'bytes':len(body),
            'sha256':hashlib.sha256(body).hexdigest()} for method, path, body in wire.events]))
        await runner.cleanup()


@asynccontextmanager
async def connected(kind, wire, monkeypatch):
    if kind == 'line':
        from plugins.platforms.line import adapter as mod
        monkeypatch.setattr(mod, 'LINE_PUSH_URL', wire.base + '/push')
        monkeypatch.setattr(mod, 'LINE_REPLY_URL', wire.base + '/reply')
        adapter = mod.LineAdapter(PlatformConfig(enabled=True))
        adapter._client = mod._LineClient('inert-local-token', timeout=1)
    elif kind == 'qqbot':
        from plugins.platforms.qqbot import adapter as mod
        monkeypatch.setattr(mod, 'API_BASE', wire.base)
        # Permit only the inert loopback peer; the real HTTPX/SSRF transport stays installed.
        from tools import url_safety
        monkeypatch.setattr(url_safety, '_global_allow_private_urls', lambda: True)
        adapter = mod.QQAdapter(PlatformConfig(enabled=True, extra={'app_id':'local-account'}))
        from plugins.platforms.qqbot.routes import ChatRoutes
        ChatRoutes(adapter._app_id).remember('peer', 'c2c')
        adapter._http_client = httpx.AsyncClient(timeout=1, trust_env=False)
        adapter._running = True
        adapter._ws = SimpleNamespace(closed=False)
        adapter._access_token = 'inert-local-token'
        adapter._token_expires_at = time.time() + 3600
    else:
        from plugins.platforms.matrix import adapter as mod
        from mautrix.client import Client
        adapter = mod.MatrixAdapter(PlatformConfig(enabled=True))
        adapter._client = Client(base_url=wire.base, mxid='@bot:local', token='inert-local-token',
                                 client_session=mod._create_matrix_session(None))
        adapter._client.api.default_retry_count = 1
        adapter._joined_rooms.add('!room:local')
    try:
        yield adapter
    finally:
        if kind == 'qqbot':
            await adapter._http_client.aclose()
        elif kind == 'matrix':
            await adapter._client.api.session.close()


def target(kind):
    return '!room:local' if kind == 'matrix' else 'Upeer' if kind == 'line' else 'peer'


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['line', 'qqbot', 'matrix'])
@pytest.mark.parametrize('fault', [None, 'lost', '503', 'redirect'])
async def test_text_native_request_single_attempt(kind, fault, monkeypatch):
    async with http_peer(fault) as wire, connected(kind, wire, monkeypatch) as adapter:
        result = await strict_call(adapter, chat_id=target(kind), content='sealed answer')
        assert result.success is (fault is None), (result, wire.events)
        assert len(wire.events) == 1, wire.events
        assert b'sealed answer' in wire.events[0][2]


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['line', 'qqbot', 'matrix'])
async def test_strict_oversize_rejected_before_any_chunk(kind, monkeypatch):
    async with http_peer() as wire, connected(kind, wire, monkeypatch) as adapter:
        result = await strict_call(adapter, chat_id=target(kind), content='x' * 100000)
        assert not result.success
        assert wire.events == []


@pytest.mark.asyncio
async def test_line_strict_ignores_memory_postback_and_has_no_reply_push_fallback(monkeypatch):
    async with http_peer('lost') as wire, connected('line', wire, monkeypatch) as adapter:
        rid = adapter._cache.register_pending('Upeer')
        adapter._pending_buttons['Upeer'] = rid
        adapter._reply_tokens['Upeer'] = ('reply-token', time.time() + 60)
        result = await strict_call(adapter, chat_id='Upeer', content='durable answer')
        assert not result.success
        assert [path for _, path, _ in wire.events] == ['/push']
        assert adapter._cache.get(rid).state.name == 'PENDING'


@pytest.mark.asyncio
async def test_line_human_reply_failure_still_falls_back_to_push(monkeypatch):
    async with http_peer('lost') as wire, connected('line', wire, monkeypatch) as adapter:
        adapter._reply_tokens['Upeer'] = ('reply-token', time.time() + 60)
        result = await adapter.send('Upeer', 'human answer')
        assert result.success
        assert [path for _, path, _ in wire.events] == ['/reply', '/push']


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['line', 'qqbot', 'matrix'])
@pytest.mark.parametrize('fault_at', [0, 1, 2])
async def test_media_native_request_stops_on_first_missing_ack(kind, fault_at, tmp_path, monkeypatch):
    path = tmp_path / 'report.bin'
    path.write_bytes(b'exact local report')
    async with http_peer('lost' if fault_at else None, fail_at=fault_at) as wire, connected(kind, wire, monkeypatch) as adapter:
        if kind == 'line':
            adapter.public_base_url = 'https://local.invalid'
            result = await strict_call(adapter, 'send_image_file', chat_id=target(kind), image_path=str(path))
            expected = 1
        elif kind == 'qqbot':
            result = await strict_call(adapter, 'send_image', chat_id=target(kind), image_url='https://local.invalid/image.png')
            expected = 2
        else:
            result = await strict_call(adapter, 'send_document', chat_id=target(kind), file_path=str(path))
            expected = 2
        failed = 0 < fault_at <= expected
        assert result.success is not failed, (result, wire.events)
        assert len(wire.events) == (fault_at if failed else expected), wire.events


@pytest.mark.asyncio
async def test_matrix_human_sdk_retries_still_work(monkeypatch):
    async with http_peer('503') as wire, connected('matrix', wire, monkeypatch) as adapter:
        # Real mautrix retry sleep is four seconds; preserve it as compatibility evidence.
        result = await adapter.send('!room:local', 'human answer')
        assert result.success
        assert len(wire.events) == 2


@pytest.mark.asyncio
async def test_qqbot_human_transient_failure_still_retries(monkeypatch):
    async with http_peer('503') as wire, connected('qqbot', wire, monkeypatch) as adapter:
        result = await adapter.send('peer', 'human answer')
        assert result.success
        assert len(wire.events) == 2
