"""Native loopback transport oracle; all credentials are synthetic."""
import importlib
import json
import os
from contextlib import asynccontextmanager
from types import SimpleNamespace

import aiohttp
from aiohttp import web
from gateway.config import PlatformConfig


@asynccontextmanager
async def peer(fault=None):
    wire = SimpleNamespace(events=[], fault=fault)

    async def handle(request):
        raw = await request.read()
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            payload = None
        event = SimpleNamespace(method=request.method, path=request.path, query=request.query_string,
                                body=raw, payload=payload, headers=dict(request.headers))
        wire.events.append(event)
        if request.path.endswith('tenant_access_token/internal'):
            return web.json_response({'code': 0, 'tenant_access_token': 'synthetic-feishu-A', 'expire': 7200})
        if request.method == 'GET':
            return web.json_response({'id': 'parent-A', 'root_id': 'parent-A'})
        index = len([e for e in wire.events if e.method == 'POST'])
        action = wire.fault(event, index) if wire.fault else None
        if action == 'lost':
            request.transport.close()
            return web.Response()
        if action == 'redirect':
            return web.Response(status=307, headers={'Location': wire.base + '/replayed'})
        if isinstance(action, int):
            return web.json_response({'error': 'synthetic rejection'}, status=action)
        if isinstance(action, dict):
            return web.json_response(action)
        if request.path.endswith('/getuploadurl'):
            return web.json_response({'ret': 0, 'upload_full_url': wire.base + '/cdn-upload'})
        if request.path == '/cdn-upload':
            return web.Response(headers={'x-encrypted-param': 'synthetic-encrypted-ref'})
        if request.path.startswith('/upload/'):
            return web.json_response({'attachmentDataRef': {'resourceName': 'uploads/ref-A'}})
        channel = payload.get('channel_id', 'channel-A') if isinstance(payload, dict) else 'channel-A'
        space = request.path.split('/spaces/', 1)[1].split('/', 1)[0] if '/spaces/' in request.path else 'space-A'
        return web.json_response({'id': f'post-{index}', 'channel_id': channel,
            'root_id': payload.get('root_id', '') if isinstance(payload, dict) else '',
            'file_infos': [{'id': f'file-{index}'}], 'code': 0,
            'data': {'message_id': f'om-{index}', 'file_key': f'file-{index}', 'image_key': f'image-{index}'},
            'name': f'spaces/{space}/messages/msg-{index}', 'ret': 0, 'errcode': 0})

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
        print('FROZEN_NATIVE_WIRE ' + json.dumps({'test': os.environ.get('PYTEST_CURRENT_TEST'),
            'events': [{'method': e.method, 'path': e.path, 'bytes': len(e.body),
                        'sha256': __import__('hashlib').sha256(e.body).hexdigest()} for e in wire.events]}))
        await runner.cleanup()


@asynccontextmanager
async def adapter_for(platform, wire):
    module = importlib.import_module(f'plugins.platforms.{platform}.adapter')
    if platform == 'feishu':
        assert module._load_lark_oapi()
        adapter = module.FeishuAdapter(PlatformConfig(extra={'app_id': 'synthetic-app-' + wire.base, 'app_secret': 'synthetic-secret'}))
        adapter._client = module._build_lark_client(adapter._app_id, 'synthetic-secret', wire.base)
        try:
            yield adapter, 'chat-A'
        finally:
            adapter._shutdown_sdk_executor()
        return
    if platform == 'google_chat':
        from google.oauth2.credentials import Credentials
        adapter = module.GoogleChatAdapter(PlatformConfig())
        from googleapiclient.discovery_cache import get_static_doc
        document = json.loads(get_static_doc('chat', 'v1'))
        document['rootUrl'] = document['baseUrl'] = wire.base + '/'
        from googleapiclient.discovery import build_from_document
        api = build_from_document(document, credentials=Credentials('synthetic-google-A'))
        adapter._chat_api = adapter._user_chat_api = api
        adapter._credentials = Credentials('synthetic-google-A')
        adapter._user_credentials = Credentials('synthetic-google-user-A')
        adapter._last_sender_by_chat = {}
        adapter._last_inbound_thread = {}
        yield adapter, 'spaces/space-A'
        return
    async with aiohttp.ClientSession(trust_env=False) as session:
        if platform == 'mattermost':
            adapter = module.MattermostAdapter(PlatformConfig())
            adapter._base_url, adapter._token, adapter._session = wire.base, 'synthetic-mattermost-A', session
            adapter._bot_user_id, adapter._reply_mode = 'bot-A', 'thread'
            chat = 'channel-A'
        else:
            adapter = module.WeixinAdapter(PlatformConfig())
            adapter._base_url, adapter._token, adapter._send_session = wire.base, 'synthetic-weixin-A', session
            adapter._cdn_base_url, adapter._account_id = wire.base, 'account-A'
            adapter._token_store._cache[adapter._token_store._key('account-A', 'peer-A')] = 'synthetic-context-A'
            chat = 'peer-A'
        yield adapter, chat


def state_for(platform, chat):
    return SimpleNamespace(platform=platform, chat_id=chat, thread_id=None, reply_to=None,
                           target={'platform': platform, 'chat_id': chat}, metadata={})


def helper(platform):
    return importlib.import_module(f'plugins.platforms.{platform}.frozen_delivery')


async def prepare(platform, adapter, state, calls, retained):
    """The exact same oracle can exercise seed native behavior before helper exists."""
    try:
        module = helper(platform)
    except ModuleNotFoundError as exc:
        if exc.name != f'plugins.platforms.{platform}.frozen_delivery':
            raise
        return {'seed': (adapter, state, calls), 'binding': {}}
    return await module.freeze(adapter, state, calls, retained)


async def deliver(platform, plan):
    if 'seed' not in plan:
        return await helper(platform).send(plan)
    from gateway.strict_transport import strict_call
    adapter, state, calls = plan['seed']
    for method, kwargs in calls:
        result = await strict_call(adapter, method, chat_id=state.chat_id, **kwargs)
        if not result.success:
            raise RuntimeError('seed ACK unknown')
    return {'ok': True, 'message_id': result.message_id}


def writes(wire):
    return [e for e in wire.events if e.method == 'POST' and 'tenant_access_token' not in e.path]
