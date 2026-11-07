"""Enterprise strict transport contracts against real local HTTP/WS peers."""
import asyncio
from collections import Counter
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from aiohttp import web, ClientSession

from gateway.config import PlatformConfig
from gateway.strict_transport import strict_transport_scope


def record_wire(wire):
    target = os.environ.get('ENTERPRISE_WIRE_RECEIPT')
    if target and not getattr(wire, '_recorded', False):
        wire._recorded = True
        counts = Counter((method, path, body.get('msgtype', '')) for method, path, body in wire.events)
        with Path(target).open('a') as stream:
            stream.write(json.dumps({'test': os.environ.get('PYTEST_CURRENT_TEST', ''),
                'total': len(wire.events), 'counts': [{'method': m, 'endpoint': p, 'msgtype': t, 'submissions': n}
                    for (m, p, t), n in sorted(counts.items())]}) + '\n')


@asynccontextmanager
async def peer(faults=None):
    faults = dict(faults or {})
    wire = SimpleNamespace(events=[], faults=faults)

    async def handle(request):
        raw = await request.read()
        try:
            body = json.loads(raw) if raw else {}
        except (ValueError, UnicodeDecodeError):
            body = {'multipart_bytes': len(raw)}
        wire.events.append((request.method, request.path, body))
        fault = wire.faults.pop(request.path, None)
        if isinstance(fault, list):
            current, *rest = fault
            if rest:
                wire.faults[request.path] = rest
            fault = current
        if fault == 'lost':
            request.transport.close()
            return web.Response()
        if isinstance(fault, int):
            return web.json_response({'error': {'message': 'rejected'}}, status=fault)
        if isinstance(fault, dict):
            if fault.get('redirect'):
                return web.Response(status=307, headers={'Location': wire.base + '/redirected'})
            return web.json_response(fault)
        if request.method == 'GET' and request.path.endswith(('.png', '.gif')):
            return web.Response(body=b'GIF89a\x01\x00\x01\x00\x00\x00\x00' if request.path.endswith('.gif') else b'\x89PNG\r\n\x1a\nproof',
                                content_type='image/gif' if request.path.endswith('.gif') else 'image/png')
        if 'tenant_access_token' in request.path:
            return web.json_response({'code': 0, 'tenant_access_token': 'inert', 'expire': 7200})
        if request.path.endswith('/images'):
            return web.json_response({'code': 0, 'data': {'image_key': 'img_peer'}})
        if request.path.endswith('/files'):
            return web.json_response({'code': 0, 'data': {'file_key': 'file_peer'}})
        if request.path.startswith('/upload/'):
            return web.json_response({'attachmentDataRef': {'resourceName': 'uploads/peer'}})
        return web.json_response({'code': 0, 'data': {'message_id': 'om_peer'}, 'name': 'spaces/peer/messages/ack', 'id': 'ack', 'errcode': 0, 'msgid': 'ack'})

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
        record_wire(wire)
        await runner.cleanup()


@asynccontextmanager
async def dingtalk(wire):
    from plugins.platforms.dingtalk.adapter import DingTalkAdapter
    adapter = DingTalkAdapter(PlatformConfig(extra={'card_template_id': 'template'}))
    async with httpx.AsyncClient(trust_env=False) as client:
        adapter._http_client = client
        adapter._session_webhooks['chat'] = (wire.base + '/ding', 0)
        adapter._message_contexts['chat'] = SimpleNamespace(conversation_type='1', sender_staff_id='inert')
        from alibabacloud_dingtalk.card_1_0.client import Client
        from alibabacloud_tea_openapi.models import Config
        adapter._card_sdk = Client(Config(endpoint=wire.base.removeprefix('http://'), protocol='http', region_id='central'))
        wire.faults['/v1.0/card/instances'] = 503
        async def token():
            return 'inert'
        adapter._get_access_token = token
        try:
            yield adapter
        finally:
            for task in adapter._bg_tasks:
                task.cancel()
            await asyncio.gather(*adapter._bg_tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', [None, 'lost', {'errcode': 500, 'errmsg': 'rejected'}])
async def test_dingtalk_strict_final_bypasses_native_card_creation(fault):
    async with peer({'/ding': fault} if fault else {}) as wire, dingtalk(wire) as adapter:
        with strict_transport_scope():
            result = await adapter.send('chat', 'sealed final', reply_to='123')
        assert result.success is (fault is None)
        assert [path for _, path, _ in wire.events] == ['/ding']
        assert not adapter._streaming_cards


@pytest.mark.asyncio
async def test_dingtalk_human_keeps_card_failure_webhook_fallback():
    async with peer() as wire, dingtalk(wire) as adapter:
        result = await adapter.send('chat', 'human')
        assert result.success
        assert [path for _, path, _ in wire.events] == ['/v1.0/card/instances', '/ding']


@asynccontextmanager
async def feishu(wire):
    from plugins.platforms.feishu import adapter as module
    assert module._load_lark_oapi()
    adapter = module.FeishuAdapter(PlatformConfig(extra={'app_id': wire.base, 'app_secret': 'inert'}))
    adapter._client = module._build_lark_client(wire.base, 'inert', wire.base)
    try:
        yield adapter
    finally:
        adapter._shutdown_sdk_executor()


def message_posts(wire):
    return [row for row in wire.events if '/messages' in row[1] and row[0] in {'POST', 'PATCH'}]


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['lost', {'code': 230001, 'msg': 'Invalid post content'}, {'code': 230011, 'msg': 'message withdrawn'}])
async def test_feishu_strict_reply_stops_retries_fallback_and_chunks(fault):
    path = '/open-apis/im/v1/messages/123/reply'
    async with peer({path: fault}) as wire, feishu(wire) as adapter:
        adapter.MAX_MESSAGE_LENGTH = 48
        with strict_transport_scope():
            result = await adapter.send('chat', '**sealed final** ' * 15, reply_to='123')
        assert not result.success
        assert len(message_posts(wire)) == 1


@pytest.mark.asyncio
async def test_feishu_human_keeps_retry_after_lost_ack():
    path = '/open-apis/im/v1/messages'
    async with peer({path: 'lost'}) as wire, feishu(wire) as adapter:
        result = await adapter.send('chat', 'human')
        assert result.success
        assert len(message_posts(wire)) == 2


@asynccontextmanager
async def wecom(wire, *, passive=True):
    from plugins.platforms.wecom.adapter import WeComAdapter

    adapter = WeComAdapter(PlatformConfig())
    async def frames(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for message in ws:
            payload = json.loads(message.data)
            wire.events.append(('WS', payload['cmd'], payload['body']))
            fault = wire.faults.pop(payload['cmd'], None)
            if isinstance(fault, list):
                current, *rest = fault
                if rest:
                    wire.faults[payload['cmd']] = rest
                fault = current
            if fault == 'lost':
                await ws.close()
                break

            extra = {'upload_id': 'up'} if 'upload_media_init' in payload['cmd'] else {'media_id': 'media'} if 'upload_media_finish' in payload['cmd'] else {}
            await ws.send_json({'headers': payload['headers'], 'errcode': fault or 0, 'body': extra})
        return ws
    app = web.Application()
    app.router.add_get('/ws', frames)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    async with ClientSession() as client:
        adapter._ws = await client.ws_connect(f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/ws')
        async def read():
            async for message in adapter._ws:
                data = json.loads(message.data)
                await adapter._dispatch_payload(data)
            for future in list(adapter._pending_responses.values()):
                if not future.done():
                    future.set_exception(TimeoutError('peer accepted, ACK lost'))
        reader = asyncio.create_task(read())
        if passive:
            adapter._remember_chat_req_id('chat', 'inbound')
        try:
            yield adapter
        finally:
            await adapter._ws.close()
            await reader
            tasks = list(adapter._chat_workers.values()) + list(adapter._control_workers.values())
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            record_wire(wire)
            await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['lost', 500])
async def test_wecom_strict_lost_ack_does_not_proactive_fallback(fault):
    from plugins.platforms.wecom.adapter import APP_CMD_RESPONSE
    wire = SimpleNamespace(events=[], faults={APP_CMD_RESPONSE: fault})
    async with wecom(wire) as adapter:
        with strict_transport_scope():
            result = await adapter.send('chat', 'sealed final')
        assert not result.success
        assert len(wire.events) == 1


@pytest.mark.asyncio
async def test_wecom_human_keeps_passive_error_proactive_fallback():
    from plugins.platforms.wecom.adapter import APP_CMD_RESPONSE, APP_CMD_SEND
    wire = SimpleNamespace(events=[], faults={APP_CMD_RESPONSE: 500})
    async with wecom(wire) as adapter:
        result = await adapter.send('chat', 'human')
        assert result.success
        assert [row[1] for row in wire.events] == [APP_CMD_RESPONSE, APP_CMD_SEND]


@asynccontextmanager
async def google_chat(wire):
    from plugins.platforms.google_chat import adapter as module
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build_from_document
    from googleapiclient.discovery_cache import get_static_doc
    assert module._load_google_modules()
    document = json.loads(get_static_doc('chat', 'v1'))
    document['rootUrl'] = wire.base + '/'
    document['baseUrl'] = wire.base + '/'
    adapter = module.GoogleChatAdapter(PlatformConfig())
    adapter._credentials = Credentials(token='inert')
    adapter._chat_api = build_from_document(json.dumps(document), credentials=adapter._credentials)
    adapter._typing_messages['spaces/peer'] = 'spaces/peer/messages/typing'
    try:
        yield adapter
    finally:
        adapter._chat_api.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['lost', 401, 503, 404])
async def test_google_strict_final_uses_create_without_typing_patch_or_retry(fault):
    async with peer({'/v1/spaces/peer/messages': fault}) as wire, google_chat(wire) as adapter:
        with strict_transport_scope():
            try:
                result = await adapter.send('spaces/peer', 'sealed final')
            except Exception:
                result = SimpleNamespace(success=False)
        assert not result.success
        assert [(method, path) for method, path, _ in wire.events] == [('POST', '/v1/spaces/peer/messages')]


@asynccontextmanager
async def teams(wire):
    from plugins.platforms.teams import adapter as module
    assert module.check_teams_requirements()
    from microsoft_teams.api import ApiClient
    from microsoft_teams.common import ClientOptions
    from microsoft_teams.apps import App
    app = App(client_id='inert', client_secret='inert', tenant_id='inert')
    app._initialized = True
    app.api = ApiClient(wire.base, ClientOptions(token='inert'))
    adapter = module.TeamsAdapter(PlatformConfig())
    adapter._app = app
    try:
        yield adapter
    finally:
        await app.api.http.http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['lost', 503])
async def test_teams_strict_reply_does_not_flat_fallback(fault):
    path = '/v3/conversations/chat;messageid=123/activities'
    async with peer({path: fault}) as wire, teams(wire) as adapter:
        with strict_transport_scope():
            result = await adapter.send('chat', 'sealed final', reply_to='123')
        assert not result.success
        assert [path for _, path, _ in wire.events] == [path]


@pytest.mark.asyncio
async def test_teams_human_keeps_reply_flat_fallback():
    path = '/v3/conversations/chat;messageid=123/activities'
    async with peer({path: 'lost'}) as wire, teams(wire) as adapter:
        result = await adapter.send('chat', 'human', reply_to='123')
        assert result.success
        assert [row[1] for row in wire.events] == [path, '/v3/conversations/chat/activities']
