"""Public strict standalone calls against real SDKs and a complete-request peer."""
from contextlib import asynccontextmanager
from email.parser import BytesParser
import hashlib
import json
from urllib.parse import parse_qs

from aiohttp import web
import pytest

from gateway.config import PlatformConfig
from tools import send_message_strict as strict


class Peer:
    def __init__(self, faults=()):
        self.faults = list(faults)
        self.events = []
        self.base = ''

    async def receive(self, request):
        body = await request.read()
        event = {'method': request.method, 'path': request.path,
                 'url': str(request.url), 'body': body.decode(errors='replace'),
                 'authorization': request.headers.get('Authorization'),
                 'body_sha256': hashlib.sha256(body).hexdigest(), 'size': len(body)}
        if 'multipart/' in request.content_type:
            envelope = b'Content-Type: ' + request.headers['Content-Type'].encode() + b'\r\n\r\n' + body
            event['parts'] = [{'name': part.get_param('name', header='content-disposition'),
                               'filename': part.get_filename(),
                               'content_type': part.get_content_type(),
                               'body': part.get_payload(decode=True).decode(errors='replace')}
                              for part in BytesParser().parsebytes(envelope).walk() if not part.is_multipart()]
        elif request.content_type == 'application/json':
            event['json'] = json.loads(body)
        else:
            event['form'] = parse_qs(body.decode(errors='replace'))
        self.events.append(event)
        fault = self.faults.pop(0) if self.faults else None
        if fault == 'lost':
            request.transport.close()
            return web.Response()
        if fault == 'redirect':
            return web.Response(status=307, headers={'Location': self.base + '/redirect-target'})
        if fault in ('503', '429'):
            return web.json_response({'ok': False, 'error': 'ratelimited', 'retry_after': 0,
                                      'global': False, 'message': 'rejected', 'code': 0},
                                     status=int(fault), headers={'Retry-After': '0'})
        if fault == 'workspace':
            return web.json_response({'ok': False, 'error': 'channel_not_found'})
        if fault == 'badack':
            if request.path == '/upload':
                return web.Response(status=204)
            return web.json_response({'ok': True, 'attachments': []})
        operation = request.path.rsplit('/', 1)[-1]
        if request.path.startswith('/bot'):
            return web.json_response({'ok': True, 'result': {'message_id': len(self.events)}})
        if operation == 'files.getUploadURLExternal':
            return web.json_response({'ok': True, 'file_id': 'F' + str(len(self.events)),
                                      'upload_url': self.base + '/upload'})
        if operation == 'upload':
            return web.Response(text='OK')
        if operation == 'files.completeUploadExternal':
            files = json.loads(request.query['files'])
            event['query'] = dict(request.query)
            return web.json_response({'ok': True, 'files': [{'id': entry['id']} for entry in files]})
        attachments = [{'id': str(i), 'filename': p['filename']} for i, p in enumerate(event.get('parts', [])) if p['filename']]
        return web.json_response({'ok': True, 'ts': str(len(self.events)) + '.001',
                                  'id': str(len(self.events)), 'attachments': attachments})


@asynccontextmanager
async def peer(faults=()):
    wire = Peer(faults)
    app = web.Application()
    app.router.add_route('*', '/{path:.*}', wire.receive)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    wire.base = 'http://127.0.0.1:' + str(site._server.sockets[0].getsockname()[1])
    try:
        yield wire
    finally:
        print('WIRE ' + json.dumps(wire.events, sort_keys=True))
        await runner.cleanup()


def route(monkeypatch, wire, platform):
    if platform == 'telegram':
        monkeypatch.setattr(strict, 'TELEGRAM_API', wire.base)
    elif platform == 'discord':
        import aiohttp
        import discord.http
        monkeypatch.setattr(discord.http.Route, 'BASE', wire.base)
        original_request = aiohttp.ClientSession._request
        async def local_request(self, method, url, **kwargs):
            url = str(url)
            if url.startswith('https://discord.com/api/v10/'):
                url = wire.base + url.removeprefix('https://discord.com/api/v10')
            return await original_request(self, method, url, **kwargs)
        monkeypatch.setattr(aiohttp.ClientSession, '_request', local_request)
    else:
        import slack_sdk.web.async_client
        original = slack_sdk.web.async_client.AsyncWebClient
        class LocalClient(original):
            def __init__(self, *args, **kwargs):
                kwargs['base_url'] = wire.base + '/'
                kwargs['proxy'] = ''
                super().__init__(*args, **kwargs)
        monkeypatch.setattr(slack_sdk.web.async_client, 'AsyncWebClient', LocalClient)


def target(platform):
    return '555' if platform == 'discord' else 'C555'


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', ['slack', 'discord'])
async def test_public_prepare_send_ack(monkeypatch, platform):
    async with peer() as wire:
        route(monkeypatch, wire, platform)
        config = PlatformConfig(token='inert-selected-profile')
        plan = strict.prepare(platform, config, target(platform), '**sealed** answer', thread_id='777')
        result = await strict.send(platform, config, target(platform), '**sealed** answer', thread_id='777', prepared_plan=plan)
        assert result.get('success'), result
        assert len(wire.events) == 1
        event = wire.events[0]
        assert event['authorization'] == ('Bot ' if platform == 'discord' else 'Bearer ') + config.token
        assert event['path'] == ('/channels/777/messages' if platform == 'discord' else '/chat.postMessage')
        if platform == 'slack':
            assert event['json']['text'] == '*sealed* answer'
            assert event['json']['thread_ts'] == '777'
            assert event['json']['channel'] == 'C555'
        else:
            assert event['json']['content'] == '**sealed** answer'
