"""Strict messaging delivery through real SDKs and inert loopback HTTP peers.

The peer consumes each complete request before dropping its ACK. No external
service or credential is used; counters observe HTTP submissions, not mocks.
"""
from contextlib import asynccontextmanager
import importlib
import hashlib
import json
import sys

# Gateway conftest installs SDK stubs without trying installed packages.
# This file runs in its own native runner child and deliberately loads the SDKs.
for _package in ('telegram', 'discord'):
    for _name in tuple(sys.modules):
        if _name == _package or _name.startswith(_package + '.'):
            del sys.modules[_name]
    importlib.import_module(_package)

from aiohttp import web
import pytest

from gateway.config import PlatformConfig
from gateway.strict_transport import strict_transport_scope, supports_strict_transport


_peers = []


@pytest.fixture(autouse=True)
def network_receipt(record_property):
    _peers.clear()
    yield
    record_property('network', json.dumps([wire.events for wire in _peers], sort_keys=True))


class Peer:
    @staticmethod
    def response(data, status=200):
        return web.Response(body=json.dumps(data).encode(), status=status, content_type='application/json')

    def __init__(self, faults=()):
        self.faults = list(faults)
        self.events = []
        self.base = ''
        _peers.append(self)

    async def handle(self, request):
        body = await request.read()
        self.events.append({'method': request.method, 'path': request.path, 'body': body.decode(errors='replace'), 'body_sha256': hashlib.sha256(body).hexdigest()})
        if request.method == 'GET' and request.path == '/image.png':
            return web.Response(body=b'inert-image', content_type='image/png')
        fault = self.faults.pop(0) if self.faults else None
        if fault == 'lost':
            request.transport.close()
            return web.Response()
        if fault == 'redirect':
            return web.Response(status=307, headers={'Location': self.base + '/redirect-target'})
        method = request.path.rsplit('/', 1)[-1]
        if fault == 'parse':
            return self.response({'ok': False, 'error_code': 400, 'description': "Bad Request: can't parse entities"}, status=400)
        if fault == 'rate':
            return self.response({'ok': False, 'error': 'ratelimited', 'error_code': 429, 'description': 'Too Many Requests', 'parameters': {'retry_after': 0.01}, 'retry_after': 0.01, 'global': False}, status=429)
        if fault == 'reference':
            return self.response({'code': 10008, 'message': 'Unknown Message'}, status=400)
        if fault == 'blocks':
            return self.response({'ok': False, 'error': 'invalid_blocks'})
        if fault == 'server':
            return self.response({'ok': False, 'code': 0, 'message': 'server error', 'error': 'internal_error'}, status=500)
        if method == 'files.getUploadURLExternal':
            return self.response({'ok': True, 'file_id': 'F' + str(len(self.events)), 'upload_url': self.base + '/upload'})
        if method == 'upload':
            return web.Response(body=b'OK', content_type='text/plain')
        if method == 'files.completeUploadExternal':
            return self.response({'ok': True, 'files': [{'id': item['id']} for item in json.loads(request.query['files'])]})
        if method.startswith('send'):
            message = {'message_id': len(self.events), 'date': 1, 'chat': {'id': 555, 'type': 'private'}, 'text': 'ACK'}
            if method == 'sendMediaGroup':
                from email.parser import BytesParser
                envelope = b'Content-Type: ' + request.headers['Content-Type'].encode() + b'\r\n\r\n' + body
                parts = BytesParser().parsebytes(envelope).walk()
                count = len(json.loads(next(part.get_payload(decode=True) for part in parts if part.get_param('name', header='content-disposition') == 'media')))
                result = [{**message, 'message_id': index + 1} for index in range(1 if fault == 'partial' else count)]
            else:
                result = message
            return self.response({'ok': True, 'result': result})
        return self.response({'ok': True, 'ts': str(len(self.events)) + '.001',
            'id': str(len(self.events)), 'channel_id': '555', 'content': 'ACK',
            'author': {'id': '888', 'username': 'peer', 'discriminator': '0000', 'avatar': None},
            'timestamp': '2026-01-01T00:00:00+00:00', 'type': 0, 'tts': False,
            'mention_everyone': False, 'attachments': [{'id': '11', 'filename': 'payload.png',
                'size': 5, 'url': self.base + '/payload.png', 'proxy_url': self.base + '/payload.png'}]
                * body.count(b'name="files[')})


@asynccontextmanager
async def peer(faults=()):
    wire = Peer(faults)
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
async def telegram_adapter(wire):
    from telegram import Bot
    from telegram.request import HTTPXRequest
    from plugins.platforms.telegram.adapter import TelegramAdapter

    request = HTTPXRequest(httpx_kwargs={'trust_env': False})
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token='123:inert-local-peer', typing_indicator=False))
    adapter._bot = Bot('123:inert-local-peer', base_url=wire.base + '/bot', request=request, get_updates_request=request)
    adapter._rich_mode = 'off'
    try:
        yield adapter
    finally:
        await request.shutdown()


@pytest.mark.asyncio
async def test_telegram_strict_parse_rejection_stops_before_fallback():
    async with peer(['parse']) as wire, telegram_adapter(wire) as adapter:
        with strict_transport_scope():
            result = await adapter.send('555', 'completed **answer**')
        assert len(wire.events) == 1, wire.events
        assert not result.success
        assert supports_strict_transport(adapter, 'send')


@pytest.mark.asyncio
async def test_telegram_normal_ack_control():
    async with peer() as wire, telegram_adapter(wire) as adapter:
        result = await adapter.send('555', 'ordinary answer')
        assert result.success
        assert len(wire.events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', [None, 'lost', 'server', 'redirect'])
async def test_telegram_strict_chunks_stop_at_first_unknown(fault):
    async with peer([fault]) as wire, telegram_adapter(wire) as adapter:
        with strict_transport_scope():
            result = await adapter.send('555', 'x' * 9000)
        assert len(wire.events) == (3 if fault is None else 1), wire.events
        assert result.success is (fault is None)
        assert result.retryable is False


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', [None, 'lost'])
async def test_telegram_strict_album_stops_without_per_image_fallback(tmp_path, fault):
    image = tmp_path / 'image.png'
    image.write_bytes(b'inert-image')
    async with peer([fault]) as wire, telegram_adapter(wire) as adapter:
        with strict_transport_scope():
            result = await adapter.send_multiple_images('555', [(image.as_uri(), 'caption')] * 11)
        assert len(wire.events) == (2 if fault is None else 1), wire.events
        assert result.success is (fault is None)
        assert supports_strict_transport(adapter, 'send_multiple_images')


@pytest.mark.asyncio
async def test_telegram_strict_album_preflights_later_missing_file(tmp_path):
    image = tmp_path / 'image.png'
    image.write_bytes(b'inert-image')
    async with peer() as wire, telegram_adapter(wire) as adapter:
        with strict_transport_scope():
            result = await adapter.send_multiple_images('555', [(image.as_uri(), '')] * 10 + [((tmp_path / 'missing.png').as_uri(), '')])
        assert wire.events == []
        assert not result.success


@pytest.mark.asyncio
@pytest.mark.parametrize('method,argument', [
    ('send_image_file', 'image_path'), ('send_video', 'video_path'),
    ('send_document', 'file_path'), ('send_voice', 'audio_path'),
])
@pytest.mark.parametrize('fault', [None, 'lost'])
async def test_telegram_strict_local_media_single_submission(tmp_path, method, argument, fault):
    media = tmp_path / 'payload.ogg'
    media.write_bytes(b'inert-media')
    async with peer([fault]) as wire, telegram_adapter(wire) as adapter:
        with strict_transport_scope():
            result = await getattr(adapter, method)('555', **{argument: str(media)}, caption='caption')
        assert len(wire.events) == 1, wire.events
        assert result.success is (fault is None)
        assert supports_strict_transport(adapter, method)


@asynccontextmanager
async def discord_adapter(wire, monkeypatch):
    import asyncio
    import aiohttp
    import discord
    from plugins.platforms.discord.adapter import DiscordAdapter

    monkeypatch.setattr(discord.http.Route, 'BASE', wire.base)
    client = discord.Client(intents=discord.Intents.none())
    await client._async_setup_hook()
    session = aiohttp.ClientSession()
    client.http._HTTPClient__session = session
    client.http.token = 'inert-local-peer'
    client.http._global_over = asyncio.Event()
    client.http._global_over.set()
    channel = discord.PartialMessageable(state=client._connection, id=555)
    client.get_channel = lambda cid: channel
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token='inert-local-peer'))
    adapter._client = client
    try:
        yield adapter
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', [None, 'lost', 'server', 'redirect'])
async def test_discord_strict_chunks_single_sdk_attempt(monkeypatch, fault):
    async with peer([fault]) as wire, discord_adapter(wire, monkeypatch) as adapter:
        with strict_transport_scope():
            result = await adapter.send('555', 'x' * 4500, reply_to='99')
        assert len(wire.events) == (3 if fault is None else 1), wire.events
        assert result.success is (fault is None)
        assert supports_strict_transport(adapter, 'send')


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', [None, 'lost'])
async def test_discord_strict_album_never_falls_back(tmp_path, monkeypatch, fault):
    image = tmp_path / 'image.png'
    image.write_bytes(b'inert-image')
    async with peer([fault]) as wire, discord_adapter(wire, monkeypatch) as adapter:
        with strict_transport_scope():
            result = await adapter.send_multiple_images('555', [(image.as_uri(), 'caption')] * 11)
        assert len(wire.events) == (2 if fault is None else 1), wire.events
        assert result.success is (fault is None)
        assert supports_strict_transport(adapter, 'send_multiple_images')


@pytest.mark.asyncio
@pytest.mark.parametrize('method,argument', [
    ('send_image_file', 'image_path'), ('send_video', 'video_path'),
    ('send_document', 'file_path'), ('send_voice', 'audio_path'),
])
@pytest.mark.parametrize('fault', [None, 'lost'])
async def test_discord_strict_local_media_single_submission(tmp_path, monkeypatch, method, argument, fault):
    media = tmp_path / 'payload.mp3'
    media.write_bytes(b'inert-media')
    async with peer([fault]) as wire, discord_adapter(wire, monkeypatch) as adapter:
        with strict_transport_scope():
            result = await getattr(adapter, method)('555', **{argument: str(media)}, caption='caption', is_voice=False) if method == 'send_voice' else await getattr(adapter, method)('555', **{argument: str(media)}, caption='caption')
        assert len(wire.events) == 1, wire.events
        assert result.success is (fault is None)
        assert supports_strict_transport(adapter, method)


@pytest.mark.asyncio
async def test_discord_strict_album_preflights_missing_later_file(tmp_path, monkeypatch):
    image = tmp_path / 'image.png'
    image.write_bytes(b'inert-image')
    async with peer() as wire, discord_adapter(wire, monkeypatch) as adapter:
        with strict_transport_scope():
            result = await adapter.send_multiple_images('555', [(image.as_uri(), '')] * 10 + [((tmp_path / 'missing.png').as_uri(), '')])
        assert wire.events == []
        assert not result.success


@asynccontextmanager
async def slack_adapter(wire):
    from types import SimpleNamespace
    from slack_sdk.web.async_client import AsyncWebClient
    from plugins.platforms.slack.adapter import SlackAdapter

    client = AsyncWebClient(token='inert-local-peer', base_url=wire.base + '/', proxy='')
    adapter = SlackAdapter(PlatformConfig(enabled=True, token='inert-local-peer'))
    adapter._app = SimpleNamespace(client=client)
    yield adapter


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', [None, 'lost', 'server', 'redirect', 'blocks'])
async def test_slack_strict_text_single_sdk_attempt(fault):
    async with peer([fault]) as wire, slack_adapter(wire) as adapter:
        with strict_transport_scope():
            result = await adapter.send('C555', 'completed **answer**', reply_to='1.001')
        assert len(wire.events) == 1, wire.events
        assert result.success is (fault is None)
        assert supports_strict_transport(adapter, 'send')


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', [None, 'lost'])
async def test_slack_strict_upload_stops_at_missing_ack(tmp_path, fault):
    media = tmp_path / 'payload.png'
    media.write_bytes(b'inert-media')
    async with peer([None, None, fault]) as wire, slack_adapter(wire) as adapter:
        with strict_transport_scope():
            result = await adapter.send_image_file('C555', str(media), caption='caption')
        assert len(wire.events) == 3, wire.events
        assert result.success is (fault is None)
        assert supports_strict_transport(adapter, 'send_image_file')


@pytest.mark.asyncio
@pytest.mark.parametrize('method,argument', [
    ('send_video', 'video_path'), ('send_document', 'file_path'), ('send_voice', 'audio_path'),
])
@pytest.mark.parametrize('fault', [None, 'lost'])
async def test_slack_strict_local_media_single_attempt(tmp_path, method, argument, fault):
    media = tmp_path / 'payload.mp3'
    media.write_bytes(b'inert-media')
    async with peer([None, None, fault]) as wire, slack_adapter(wire) as adapter:
        with strict_transport_scope():
            result = await getattr(adapter, method)('C555', **{argument: str(media)}, caption='caption')
        assert len(wire.events) == 3, wire.events
        assert result.success is (fault is None)
        assert supports_strict_transport(adapter, method)


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', [None, 'lost'])
async def test_slack_strict_album_stops_before_next_batch(tmp_path, fault):
    media = tmp_path / 'payload.png'
    media.write_bytes(b'inert-media')
    # Each ten-file batch does ten URL, ten byte-upload and one complete POST.
    async with peer([None] * 20 + [fault]) as wire, slack_adapter(wire) as adapter:
        with strict_transport_scope():
            result = await adapter.send_multiple_images('C555', [(media.as_uri(), 'caption')] * 11)
        assert len(wire.events) == (24 if fault is None else 21), wire.events
        assert result.success is (fault is None)
        assert supports_strict_transport(adapter, 'send_multiple_images')


@pytest.mark.asyncio
async def test_slack_strict_album_preflights_missing_later_file(tmp_path):
    image = tmp_path / 'image.png'
    image.write_bytes(b'inert-image')
    async with peer() as wire, slack_adapter(wire) as adapter:
        with strict_transport_scope():
            result = await adapter.send_multiple_images('C555', [(image.as_uri(), '')] * 10 + [((tmp_path / 'missing.png').as_uri(), '')])
        assert wire.events == []
        assert not result.success


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', [0, 1, 2])
@pytest.mark.parametrize('fault', ['lost', 'redirect', 'server'])
async def test_slack_strict_upload_each_phase_aborts(tmp_path, phase, fault):
    media = tmp_path / 'payload.png'
    media.write_bytes(b'inert-media')
    async with peer([None] * phase + [fault]) as wire, slack_adapter(wire) as adapter:
        with strict_transport_scope():
            result = await adapter.send_image_file('C555', str(media))
        assert len(wire.events) == phase + 1, wire.events
        assert not result.success


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', ['telegram', 'discord', 'slack'])
@pytest.mark.parametrize('fault', [None, 'lost'])
async def test_strict_public_image_url(tmp_path, monkeypatch, platform, fault):
    media = tmp_path / 'payload.png'
    media.write_bytes(b'inert-media')
    async with peer([None, None, fault] if platform == 'slack' else [fault]) as wire:
        factory = telegram_adapter(wire) if platform == 'telegram' else discord_adapter(wire, monkeypatch) if platform == 'discord' else slack_adapter(wire)
        async with factory as adapter:
            with strict_transport_scope():
                result = await adapter.send_image('C555' if platform == 'slack' else '555', media.as_uri(), caption='caption')
            assert len(wire.events) == (3 if platform == 'slack' else 1), wire.events
            assert result.success is (fault is None)
            assert supports_strict_transport(adapter, 'send_image')


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', [None, 'lost'])
async def test_discord_strict_native_voice(tmp_path, monkeypatch, fault):
    # Minimal single-page Ogg/Opus stream accepted by the adapter's duration parser.
    header = bytearray(28)
    header[:4] = b'OggS'
    header[6:14] = (48000).to_bytes(8, 'little')
    header[26:28] = bytes([1, 19])
    media = tmp_path / 'voice.ogg'
    media.write_bytes(header + b'OpusHead' + bytes([1, 1]) + bytes(9))
    async with peer([fault]) as wire, discord_adapter(wire, monkeypatch) as adapter:
        with strict_transport_scope():
            result = await adapter.send_voice('555', str(media), is_voice=True)
        assert len(wire.events) == 1, wire.events
        assert result.success is (fault is None)
        assert '8192' in wire.events[0]['body']


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', ['telegram', 'discord', 'slack'])
@pytest.mark.parametrize('fault', [None, 'lost'])
async def test_common_background_recovery_never_replays_unknown(tmp_path, monkeypatch, platform, fault):
    from types import SimpleNamespace
    from gateway import background_messaging_results as durable
    from gateway.config import Platform
    from gateway.session import SessionSource
    from hermes_state import SessionDB
    from hermes_state_background import _key, _load

    async with peer([fault]) as wire:
        factory = telegram_adapter(wire) if platform == 'telegram' else discord_adapter(wire, monkeypatch) if platform == 'discord' else slack_adapter(wire)
        async with factory as adapter:
            db = SessionDB(tmp_path / 'state.db')
            runner = SimpleNamespace(_session_db=db, _delivery_adapter_for=lambda source: adapter)
            source = SessionSource(platform=Platform(platform), chat_id='C555' if platform == 'slack' else '555')
            try:
                context = await durable.prepare(runner, 'inert-work', source, 'bg_sdk', [], [])
                await durable.seal(context, 'bg_sdk', 'sealed result')
                assert await durable.recover(runner, db) == (1 if fault is None else 0)
                assert len(wire.events) == 1, wire.events
                assert db._execute_write(lambda conn: _load(conn, _key('bg_sdk')))['delivery'] == ('delivered' if fault is None else 'unknown')
                assert await durable.recover(runner, db) == 0
                assert len(wire.events) == 1, wire.events
            finally:
                db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', ['telegram', 'discord', 'slack'])
async def test_strict_later_chunk_unknown_aborts_followers(monkeypatch, platform):
    async with peer([None, 'lost']) as wire:
        factory = telegram_adapter(wire) if platform == 'telegram' else discord_adapter(wire, monkeypatch) if platform == 'discord' else slack_adapter(wire)
        async with factory as adapter:
            adapter.MAX_MESSAGE_LENGTH = 80
            with strict_transport_scope():
                result = await adapter.send('C555' if platform == 'slack' else '555', 'x' * 200)
            assert len(wire.events) == 2, wire.events
            assert not result.success
            assert len(result.raw_response['message_ids']) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', ['telegram', 'discord', 'slack'])
async def test_strict_rate_rejection_stops_single_post(monkeypatch, platform):
    async with peer(['rate']) as wire:
        factory = telegram_adapter(wire) if platform == 'telegram' else discord_adapter(wire, monkeypatch) if platform == 'discord' else slack_adapter(wire)
        async with factory as adapter:
            with strict_transport_scope():
                result = await adapter.send('C555' if platform == 'slack' else '555', 'sealed result')
            assert len(wire.events) == 1, wire.events
            assert not result.success and not result.retryable


@pytest.mark.asyncio
async def test_slack_strict_chunks_ack_control():
    async with peer() as wire, slack_adapter(wire) as adapter:
        adapter.MAX_MESSAGE_LENGTH = 80
        with strict_transport_scope():
            result = await adapter.send('C555', 'x' * 200)
        assert len(wire.events) == 3, wire.events
        assert result.success


@pytest.mark.asyncio
async def test_telegram_human_parse_fallback_keeps_two_real_posts():
    async with peer(['parse']) as wire, telegram_adapter(wire) as adapter:
        result = await adapter.send('555', '**ordinary** answer')
        assert result.success
        assert len(wire.events) == 2, wire.events


@pytest.mark.asyncio
async def test_slack_human_sdk_lost_ack_keeps_retry():
    async with peer(['lost']) as wire, slack_adapter(wire) as adapter:
        result = await adapter.send('C555', 'ordinary answer')
        assert result.success
        assert len(wire.events) == 2, wire.events


@pytest.mark.asyncio
async def test_discord_human_sdk_5xx_keeps_retry(monkeypatch):
    async with peer(['server']) as wire, discord_adapter(wire, monkeypatch) as adapter:
        result = await adapter.send('555', 'ordinary answer')
        assert result.success
        assert len(wire.events) == 2, wire.events


@pytest.mark.asyncio
async def test_slack_rich_blocks_strict_no_fallback_human_keeps_fallback():
    async with peer(['blocks']) as wire, slack_adapter(wire) as adapter:
        adapter.config.extra['rich_blocks'] = True
        with strict_transport_scope():
            result = await adapter.send('C555', '# Completed\n\n- answer')
        assert not result.success and len(wire.events) == 1
        assert 'blocks' in json.loads(wire.events[0]['body'])
    async with peer(['blocks']) as wire, slack_adapter(wire) as adapter:
        adapter.config.extra['rich_blocks'] = True
        result = await adapter.send('C555', '# Completed\n\n- answer')
        assert result.success and len(wire.events) == 2
        assert 'blocks' not in json.loads(wire.events[1]['body'])


@pytest.mark.asyncio
async def test_discord_strict_deleted_reference_has_no_plain_retry(monkeypatch):
    async with peer(['reference']) as wire, discord_adapter(wire, monkeypatch) as adapter:
        with strict_transport_scope():
            result = await adapter.send('555', 'sealed', reply_to='99')
        assert not result.success and len(wire.events) == 1
        assert json.loads(wire.events[0]['body'])['message_reference']['message_id'] == 99


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', ['telegram', 'discord'])
@pytest.mark.parametrize('fault', [None, 'lost'])
async def test_strict_animation_single_attempt(tmp_path, monkeypatch, platform, fault):
    media = tmp_path / 'payload.gif'
    media.write_bytes(b'inert-animation')
    async with peer([fault]) as wire:
        factory = telegram_adapter(wire) if platform == 'telegram' else discord_adapter(wire, monkeypatch)
        async with factory as adapter:
            with strict_transport_scope():
                result = await adapter.send_animation('555', media.as_uri(), caption='caption')
            assert len(wire.events) == 1, wire.events
            assert result.success is (fault is None)
            assert supports_strict_transport(adapter, 'send_animation')


@pytest.mark.asyncio
async def test_unsupported_discord_forum_refuses_before_post(monkeypatch):
    import discord
    async with peer() as wire, discord_adapter(wire, monkeypatch) as adapter:
        channel = discord.PartialMessageable(state=adapter._client._connection, id=555, type=discord.ChannelType.forum)
        adapter._client.get_channel = lambda cid: channel
        with strict_transport_scope():
            result = await adapter.send('555', 'sealed')
        assert not result.success and wire.events == []
        assert 'forum parent' in result.error


@pytest.mark.asyncio
async def test_unsupported_slack_dm_refuses_before_post():
    async with peer() as wire, slack_adapter(wire) as adapter:
        with strict_transport_scope():
            result = await adapter.send('U555', 'sealed')
        assert not result.success and wire.events == []
        assert 'unresolved Slack DM' in result.error


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', ['telegram', 'discord', 'slack'])
@pytest.mark.parametrize('fault', [None, 'lost'])
async def test_strict_remote_image_real_download(monkeypatch, platform, fault):
    from tools import url_safety
    from urllib.parse import urlsplit
    async with peer([None, None, fault] if platform == 'slack' else [fault]) as wire:
        url = wire.base + '/image.png'
        original = url_safety._SSRFGuardedBackendBase._connect_ips
        allowed = urlsplit(wire.base)
        # Permit this loopback peer only; actual HTTPX DNS/connect and GET stay real.
        monkeypatch.setattr(url_safety, 'is_safe_url', lambda candidate: candidate == url)
        monkeypatch.setattr(url_safety._SSRFGuardedBackendBase, '_connect_ips',
            lambda self, host, port: ['127.0.0.1'] if (host, port) == (allowed.hostname, allowed.port) else original(self, host, port))
        factory = telegram_adapter(wire) if platform == 'telegram' else discord_adapter(wire, monkeypatch) if platform == 'discord' else slack_adapter(wire)
        async with factory as adapter:
            with strict_transport_scope():
                result = await adapter.send_image('C555' if platform == 'slack' else '555', url, caption='caption')
            assert len([e for e in wire.events if e['method'] == 'POST']) == (3 if platform == 'slack' else 1), wire.events
            assert len([e for e in wire.events if e['method'] == 'GET']) == (0 if platform == 'telegram' else 1)
            assert result.success is (fault is None)


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', ['telegram', 'discord', 'slack'])
@pytest.mark.parametrize('fault', [None, 'lost'])
async def test_common_final_image_dispatch_counts_and_no_replay(tmp_path, monkeypatch, platform, fault):
    from types import SimpleNamespace
    from gateway import pending_final as pf
    from gateway.config import Platform
    from gateway.final_delivery import send_attachments
    from gateway.platforms.event import MessageEvent
    from gateway.session import SessionSource
    from gateway.session_delivery_policy import automation_delivery_scope
    from gateway.session_results import finish_result
    from hermes_state import SessionDB
    from hermes_state_runtime import admit_session_input, begin_runtime_epoch, claim_session_input
    from tools import url_safety
    from urllib.parse import urlsplit

    image = tmp_path / 'payload.png'
    image.write_bytes(b'inert-image')
    async with peer([None, None, fault] if platform == 'slack' else [fault]) as wire:
        factory = telegram_adapter(wire) if platform == 'telegram' else discord_adapter(wire, monkeypatch) if platform == 'discord' else slack_adapter(wire)
        async with factory as adapter:
            db = SessionDB(tmp_path / 'state.db')
            db.create_session('sid', source=platform)
            epoch = begin_runtime_epoch(db, instance_id='inert-owner')
            route = 'agent:main:' + platform + ':dm:peer'
            source = SessionSource(platform=Platform(platform), chat_id='C555' if platform == 'slack' else '555', chat_type='dm')
            payload = {'text': 'work', 'native_text_v1': {'route': route, 'source': source.to_dict(),
                'automation': {'owner': 'sid', 'identity': 'saved-image', 'result_recovery_v1': 'consume_only'}}}
            admit_session_input(db, epoch=epoch, principal_id='automation:' + route, session_id='sid', request_id='saved-image', payload=payload)
            row = claim_session_input(db, epoch=epoch, session_id='sid')
            event = MessageEvent(text='work', source=source, internal=True, message_id='saved-image')
            authority = SimpleNamespace(db=db, runner=SimpleNamespace(_adapter_for_source=lambda source: adapter))
            image_url = wire.base + '/image.png'
            original = url_safety._SSRFGuardedBackendBase._connect_ips
            allowed = urlsplit(wire.base)
            monkeypatch.setattr(url_safety, 'is_safe_url', lambda url: url == image_url)
            monkeypatch.setattr(url_safety._SSRFGuardedBackendBase, '_connect_ips',
                lambda self, host, port: ['127.0.0.1'] if (host, port) == (allowed.hostname, allowed.port) else original(self, host, port))
            response = '![proof](' + image_url + ')'
            try:
                async with automation_delivery_scope(authority, row, event):
                    prepared = await pf.prepare(authority, row, event, route, response)
                assert prepared is not None
                finish_result(db, epoch=epoch, row=row, response=response, outcome='completed',
                    result={'result': {'final_response': response, 'messages': [], '_pending_final_v1': prepared}, 'usage': {}})
                extracted = await adapter._extract_response_content(response, event, route, is_ephemeral_response=False)
                outcomes = []
                await send_attachments(adapter, event, extracted, prepared['metadata'], anything_sent=False, record_delivery=outcomes.append)
                assert len(outcomes) == 1 and outcomes[0].success is (fault is None)
                part = pf.pending(db)[0]['parts']['attachments']
                assert (part['state'], part['attempts']) == ('delivered' if fault is None else 'unknown', 1)
                before = list(wire.events)
                assert len([e for e in before if e['method'] == 'POST']) == (3 if platform == 'slack' else 1), before
                await send_attachments(adapter, event, extracted, prepared['metadata'], anything_sent=False, record_delivery=outcomes.append)
                assert wire.events == before
            finally:
                db.close()


@pytest.mark.asyncio
async def test_telegram_strict_partial_album_ack_stops_following_batch(tmp_path):
    media = tmp_path / 'payload.png'
    media.write_bytes(b'inert-media')
    async with peer(['partial']) as wire, telegram_adapter(wire) as adapter:
        with strict_transport_scope():
            result = await adapter.send_multiple_images('555', [(media.as_uri(), '')] * 11)
        assert len(wire.events) == 1
        assert not result.success


@pytest.mark.asyncio
async def test_strict_scope_and_human_retry_do_not_mutate_shared_sdk():
    import asyncio
    async with peer(['lost']) as strict_wire, peer(['lost']) as human_wire:
        async with slack_adapter(strict_wire) as strict_adapter, slack_adapter(human_wire) as human_adapter:
            original_handlers = list(human_adapter._app.client.retry_handlers)
            async def strict_send():
                with strict_transport_scope():
                    return await strict_adapter.send('C555', 'sealed')
            strict_result, human_result = await asyncio.gather(strict_send(), human_adapter.send('C555', 'ordinary'))
            assert not strict_result.success and human_result.success
            assert len(strict_wire.events) == 1 and len(human_wire.events) == 2
            assert human_adapter._app.client.retry_handlers == original_handlers
