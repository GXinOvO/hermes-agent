"""Real SDK fixtures and local HTTP observations for strict thread contracts."""
from contextlib import asynccontextmanager
from email.parser import BytesParser
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote
import hashlib
import importlib
import importlib.util
import json
import sys

# gateway/conftest installs stubs; these tests need the installed native SDKs.
for _package in ('telegram', 'discord'):
    for _name in tuple(sys.modules):
        if _name == _package or _name.startswith(_package + '.'):
            del sys.modules[_name]
    importlib.import_module(_package)

import aiohttp
from aiohttp import web


def module(name):
    path = f'plugins.platforms.{name}.strict_thread'
    assert importlib.util.find_spec(path) is not None, f'missing strict native helper: {name}'
    return importlib.import_module(path)


def _fields(request, raw):
    if request.content_type == 'application/json':
        return json.loads(raw)
    if request.content_type == 'application/x-www-form-urlencoded':
        return {k: v[0] for k, v in parse_qs(raw.decode()).items()}
    fields = {}
    if request.content_type == 'multipart/form-data':
        mime = BytesParser().parsebytes(b'Content-Type: ' + request.headers['Content-Type'].encode() + b'\r\n\r\n' + raw)
        for part in mime.walk():
            key = part.get_param('name', header='content-disposition')
            if key:
                value = part.get_payload(decode=True)
                fields[key] = {'sha256': hashlib.sha256(value).hexdigest(), 'filename': part.get_filename()} if part.get_filename() else value.decode()
    return fields


def _telegram(path, fields, fault, index):
    if path.endswith('createForumTopic'):
        result = {'message_thread_id': 777, 'name': fields['name'], 'icon_color': 7322096}
        if fault == 'wrong_parent':
            result['chat_id'] = 999
        if fault == 'wrong_root':
            result['message_thread_id'] = True
        if fault == 'wrong_name':
            result['name'] = 'foreign'
    else:
        result = _telegram_message(path, fields, fault, index)
    data = {'ok': True, 'result': result}
    if fault == 'shape':
        data['ok'] = 1
    return data


def _telegram_message(path, fields, fault, index):
    result = {'message_id': index, 'date': 1, 'chat': {'id': 555, 'type': 'private'}, 'text': fields.get('text', '')}
    if 'message_thread_id' in fields:
        result['message_thread_id'] = int(fields['message_thread_id'])
    kind = path.rsplit('/', 1)[-1][4:].lower()
    if kind == 'photo':
        result['photo'] = [{'file_id': 'local-file', 'file_unique_id': 'local-unique', 'width': 1, 'height': 1}]
    elif kind in ('video', 'document', 'voice', 'audio'):
        result[kind] = {'file_id': 'local-file', 'file_unique_id': 'local-unique', 'duration': 1, 'width': 1, 'height': 1}
    if fault == 'wrong_parent':
        result['chat']['id'] = 999
    if fault == 'wrong_root':
        result['message_thread_id'] = 999
    return result


def _discord(path, fields, fault, index):
    if path.endswith('/threads'):
        data = {'id': '777', 'parent_id': '555', 'guild_id': '444', 'owner_id': '123', 'type': 11,
                'message_count': 0, 'member_count': 1,
                'name': fields['name'], 'thread_metadata': {'archived': False, 'locked': False,
                'auto_archive_duration': 1440, 'archive_timestamp': '2026-01-01T00:00:00Z'}}
        replacements = {'wrong_parent': ('parent_id', '999'), 'wrong_scope': ('guild_id', '999'),
                        'wrong_root': ('id', True), 'wrong_name': ('name', 'foreign')}
        if fault in replacements:
            key, value = replacements[fault]
            data[key] = value
    else:
        payload = json.loads(fields['payload_json']) if 'payload_json' in fields else fields
        data = {'id': str(1000 + index), 'channel_id': path.split('/')[2], 'content': payload.get('content', ''),
                'attachments': payload.get('attachments', [])}
        if fault == 'wrong_parent':
            data['channel_id'] = '999'
        if fault == 'wrong_root':
            data['id'] = True
    return [] if fault == 'shape' else data


def _matrix(request, fields, fault, index, stored):
    if request.method == 'PUT':
        event_id = '$root' if index == 1 else '$body' + str(index)
        room = unquote(request.path.split('/rooms/')[1].split('/')[0])
        stored[event_id] = {'event_id': event_id, 'room_id': room, 'sender': '@bot:local',
                           'type': 'm.room.message', 'content': fields, 'origin_server_ts': 1}
        data = {'event_id': True if fault == 'wrong_root' else event_id}
    else:
        event_id = unquote(request.path.rsplit('/', 1)[-1])
        data = dict(stored[event_id])
        replacements = {'wrong_parent': ('room_id', '!foreign:local'), 'wrong_scope': ('sender', '@foreign:local'),
            'wrong_name': ('content', {'msgtype': 'm.text', 'body': 'foreign'}), 'wrong_root': ('event_id', '$foreign')}
        if fault in replacements:
            key, value = replacements[fault]
            data[key] = value
    return [] if fault == 'shape' else data


class Peer:
    def __init__(self, platform, faults=()):
        self.platform, self.faults = platform, list(faults)
        self.events, self.stored = [], {}
        self.base = ''

    async def handle(self, request):
        raw = await request.read()
        fields = _fields(request, raw)
        self.events.append({'method': request.method, 'path': request.path, 'authorization': request.headers.get('Authorization'),
                            'fields': fields, 'body_sha256': hashlib.sha256(raw).hexdigest()})
        index = len(self.events)
        fault = self.faults.pop(0) if self.faults else None
        if fault == 'lost':
            request.transport.close()
            return web.Response()
        if fault == 'redirect':
            return web.Response(status=307, headers={'Location': self.base + '/redirected'})
        if fault in ('rate', 'server'):
            return web.json_response({'ok': False, 'error_code': 429 if fault == 'rate' else 503,
                'description': 'local fault', 'parameters': {'retry_after': 0}, 'message': 'local fault',
                'errcode': 'M_LIMIT_EXCEEDED', 'error': 'local fault'}, status=429 if fault == 'rate' else 503)
        if self.platform == 'telegram':
            data = _telegram(request.path, fields, fault, index)
        elif self.platform == 'discord':
            data = _discord(request.path, fields, fault, index)
        else:
            data = _matrix(request, fields, fault, index, self.stored)
        return web.json_response(data)


@asynccontextmanager
async def peer(platform, faults=()):
    wire = Peer(platform, faults)
    app = web.Application()
    app.router.add_route('*', '/{path:.*}', wire.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    wire.base = 'http://127.0.0.1:' + str(site._server.sockets[0].getsockname()[1])
    try:
        yield wire
    finally:
        await runner.cleanup()


@asynccontextmanager
async def connected(name, wire, monkeypatch):
    from gateway.config import PlatformConfig
    if name == 'telegram':
        from telegram import Bot
        from telegram.request import HTTPXRequest
        from plugins.platforms.telegram.adapter import TelegramAdapter
        request = HTTPXRequest(httpx_kwargs={'trust_env': False})
        adapter = TelegramAdapter(PlatformConfig(enabled=True, token='123:inert-local-peer', typing_indicator=False))
        adapter._bot = Bot('123:inert-local-peer', base_url=wire.base + '/bot', request=request, get_updates_request=request)
        adapter._rich_mode = 'off'
        parent, scope = '555', ''
        async def close():
            await request.shutdown()
    elif name == 'discord':
        import discord
        from plugins.platforms.discord.adapter import DiscordAdapter
        monkeypatch.setattr(discord.http.Route, 'BASE', wire.base)
        client = discord.Client(intents=discord.Intents.none())
        await client._async_setup_hook()
        client.http.token = 'inert-local-peer'
        client._connection.user = discord.ClientUser(state=client._connection, data={
            'id': '123', 'username': 'bot', 'discriminator': '0000', 'avatar': None})
        guild = discord.Guild(state=client._connection, data={'id': '444', 'name': 'local'})
        channel = discord.TextChannel(state=client._connection, guild=guild, data={
            'id': '555', 'name': 'parent', 'type': 0, 'position': 0})
        client.get_channel = lambda cid: channel if cid == 555 else None
        adapter = DiscordAdapter(PlatformConfig(enabled=True, token='inert-local-peer'))
        adapter._client = client
        parent, scope = '555', '444'
        close = client.close
    else:
        from mautrix.api import HTTPAPI
        from mautrix.client import Client
        from mautrix.types import UserID
        from plugins.platforms.matrix.adapter import MatrixAdapter
        session = aiohttp.ClientSession(trust_env=False)
        api = HTTPAPI(wire.base, token='inert-local-peer', client_session=session)
        adapter = MatrixAdapter(PlatformConfig(enabled=True))
        adapter._client = Client(mxid=UserID('@bot:local'), api=api)
        adapter._encryption = False
        adapter._proxy_url = None
        parent, scope = '!room:local', 'local'
        close = session.close
    state = SimpleNamespace(platform_name=name, chat_id=parent, thread_id=None,
        job={'id': 'job', 'name': 'native proof', 'execution_id': 'exec'},
        origin={'scope_id': scope}, target_chat_type='group' if name == 'discord' else 'dm', is_dm_target=name != 'discord')
    try:
        yield adapter, state
    finally:
        await close()
