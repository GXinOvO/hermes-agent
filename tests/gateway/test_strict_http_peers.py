"""Real loopback peers for HTTP/SMTP strict-send qualification."""
import asyncio
import importlib
import json
import os
from collections import Counter
from contextlib import asynccontextmanager
from types import SimpleNamespace

import aiohttp
from aiohttp import web
import httpx

from gateway.config import PlatformConfig


@asynccontextmanager
async def http_peer(fault=None):
    records = []

    async def receive(request):
        body = await request.read()
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            payload = None
        record = SimpleNamespace(path=request.path, method=request.method, body=body, payload=payload)
        records.append(record)
        action = fault(record, records) if fault else None
        if isinstance(action, web.StreamResponse):
            return action
        if action == 'empty-chats':
            return web.json_response({'data': []})
        if action == 'drop':
            request.transport.close()
            return web.Response()
        if action == 'missing':
            return web.json_response({})
        if action == 'reject':
            return web.json_response({'message': 'invalid root_id: post missing'}, status=400)
        if request.method == 'GET':
            if request.path.endswith('.png'):
                return web.Response(body=b'\x89PNG\r\n\x1a\n' + b'\0' * 24, content_type='image/png')
            return web.json_response({'id': 'root', 'root_id': 'root'})
        n = len(records)
        return web.json_response({'id': f'id-{n}', 'sid': f'SM-{n}', 'status': 200,
            'data': {'guid': f'guid-{n}', 'chatGuid': 'iMessage;-;inert'}, 'file_infos': [{'id': f'file-{n}'}],
            'messages': [{'id': f'wamid-{n}'}], 'messageId': f'bridge-{n}',
            'result': {'timestamp': n, 'results': [{'type': 'SUCCESS'}]}})

    app = web.Application()
    app.router.add_route('*', '/{tail:.*}', receive)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield SimpleNamespace(url=f'http://127.0.0.1:{port}', port=port, records=records)
    finally:
        print('STRICT_HTTP_TRANSPORT ' + json.dumps({'test': os.environ.get('PYTEST_CURRENT_TEST'), 'kind': 'http',
            'requests': dict(Counter(r.method + ' ' + r.path + (' ' + r.payload['method'] if isinstance(r.payload, dict) and 'method' in r.payload else '') for r in records))}, sort_keys=True))
        await runner.cleanup()


@asynccontextmanager
async def adapter_for(name, peer, monkeypatch):
    config = PlatformConfig(enabled=True, extra={})
    async with httpx.AsyncClient(trust_env=False, timeout=2) as hx, aiohttp.ClientSession(trust_env=False) as ah:
        specs = {
            'bluebubbles': ('BlueBubblesAdapter', {'server_url': peer.url, 'password': 'inert', 'client': hx}, 'iMessage;-;inert'),
            'mattermost': ('MattermostAdapter', {'_base_url': peer.url, '_session': ah, '_token': 'inert'}, 'channel'),
            'ntfy': ('NtfyAdapter', {'_server': peer.url, '_http_client': hx, '_publish_topic': 'topic'}, 'topic'),
            'sms': ('SmsAdapter', {'_account_sid': 'inert', '_auth_token': 'inert', '_from_number': '+15550000000', '_http_session': ah}, '+15550000001'),
            'whatsapp_cloud': ('WhatsAppCloudAdapter', {'_http_client': hx, '_phone_number_id': 'phone', '_access_token': 'inert'}, '15550000001'),
            'signal': ('SignalAdapter', {'account': '+15550000000', 'http_url': peer.url, 'client': hx}, 'recipient-uuid'),
        }
        cls, settings, chat = specs[name]
        module = importlib.import_module(f'plugins.platforms.{name}.adapter')
        adapter = getattr(module, cls)(config)
        for attr, value in settings.items():
            setattr(adapter, attr, value)
        constants = {'sms': ('TWILIO_API_BASE', peer.url + '/Accounts'), 'whatsapp_cloud': ('GRAPH_API_BASE', peer.url)}
        if name in constants:
            monkeypatch.setattr(module, *constants[name])
        yield adapter, chat


@asynccontextmanager
async def smtp_peer(drop_first=False):
    messages = []

    async def receive(reader, writer):
        writer.write(b'220 loopback SMTP\r\n')
        await writer.drain()
        try:
            while line := await reader.readline():
                command = line.split(b' ', 1)[0].strip().upper()
                if command == b'DATA':
                    writer.write(b'354 end with dot\r\n')
                    await writer.drain()
                    body = []
                    while (line := await reader.readline()) and line != b'.\r\n':
                        body.append(line)
                    messages.append(b''.join(body))
                    if drop_first and len(messages) == 1:
                        return
                    writer.write(b'250 queued\r\n')
                elif command == b'QUIT':
                    writer.write(b'221 bye\r\n')
                    await writer.drain()
                    return
                else:
                    response = {b'EHLO': b'250-loopback\r\n250 AUTH PLAIN\r\n',
                                b'HELO': b'250-loopback\r\n250 AUTH PLAIN\r\n',
                                b'AUTH': b'235 authenticated\r\n'}
                    writer.write(response.get(command, b'250 ok\r\n'))
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(receive, '127.0.0.1', 0)
    peer = SimpleNamespace(port=server.sockets[0].getsockname()[1], messages=messages)
    try:
        yield peer
    finally:
        print('STRICT_HTTP_TRANSPORT ' + json.dumps({'test': os.environ.get('PYTEST_CURRENT_TEST'), 'kind': 'smtp',
                                                   'accepted_DATA': len(messages)}, sort_keys=True))
        server.close()
        await server.wait_closed()


def email_for(peer):
    from plugins.platforms.email.adapter import EmailAdapter
    adapter = EmailAdapter(PlatformConfig(extra={'address': 'sender@inert.invalid', 'smtp_host': '127.0.0.1'}))
    adapter._smtp_port, adapter._smtp_security, adapter._password = peer.port, 'plain', 'inert'
    return adapter
