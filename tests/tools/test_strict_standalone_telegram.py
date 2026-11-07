"""Telegram frozen options and strict/manual transport boundaries."""
import json

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.strict_transport import strict_transport_scope
from tools import send_message_strict as strict
from tools.send_message_tool import _send_to_platform
from tests.tools import test_strict_standalone_dispatch as fixtures


@pytest.mark.asyncio
async def test_telegram_freezes_proxy_format_types_voice_and_all_bytes(tmp_path, monkeypatch):
    from gateway.platforms import base
    from tools import send_message_senders as senders
    files = []
    for name, voice in [('photo.png', False), ('clip.mp4', False), ('voice.ogg', True), ('song.mp3', False), ('report.txt', False)]:
        file = tmp_path / name
        file.write_bytes(('original ' + name).encode())
        files.append((str(file), voice))
    async with fixtures.peer() as wire:
        fixtures.route(monkeypatch, wire, 'telegram')
        monkeypatch.setattr(base, 'resolve_proxy_url', lambda *args, **kwargs: None)
        config = PlatformConfig(token='inert-original', extra={'disable_link_previews': True})
        plan = strict.prepare('telegram', config, '555', '<b>sealed</b>', media_files=files, thread_id='777')
        config.token = 'inert-drift'
        config.extra['disable_link_previews'] = False
        for path, _voice in files:
            __import__('pathlib').Path(path).unlink()
        def forbidden(*args, **kwargs):
            raise AssertionError('send reread a prepared option')
        monkeypatch.setattr(base, 'resolve_proxy_url', forbidden)
        monkeypatch.setattr(senders, '_telegram_format', forbidden)
        result = await strict.send('telegram', config, '555', '<b>sealed</b>', media_files=files,
                                   thread_id='777', prepared_plan=plan)
        assert result.get('success'), result
        assert [event['path'].rsplit('/', 1)[-1] for event in wire.events] == [
            'sendMessage', 'sendPhoto', 'sendVideo', 'sendVoice', 'sendAudio', 'sendDocument']
        assert wire.events[0]['form']['parse_mode'] == ['HTML']
        assert wire.events[0]['form']['text'] == ['<b>sealed</b>']
        assert wire.events[0]['form']['disable_web_page_preview'] == ['true']
        content_types = ['image/png', 'video/mp4', 'audio/ogg', 'audio/mpeg', 'text/plain']
        for event, (path, _voice), content_type in zip(wire.events[1:], files, content_types):
            assert event['path'].startswith('/botinert-original/')
            parts = event['parts']
            assert next(part['body'] for part in parts if part['name'] == 'message_thread_id') == '777'
            uploaded = next(part for part in parts if part['filename'])
            name = __import__('pathlib').Path(path).name
            assert uploaded['filename'] == name and uploaded['body'] == 'original ' + name
            assert uploaded['content_type'] == content_type
        print('TELEGRAM_FROZEN ' + json.dumps(wire.events))


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['lost', '429', '503', 'redirect', 'badack'])
async def test_telegram_media_unknown_ack_stops_later_files_and_reconsume(tmp_path, monkeypatch, fault):
    files = []
    for name in ['first.txt', 'later.txt']:
        path = tmp_path / name
        path.write_text('sealed ' + name)
        files.append((str(path), False))
    async with fixtures.peer([fault]) as wire:
        fixtures.route(monkeypatch, wire, 'telegram')
        config = PlatformConfig(token='inert')
        plan = strict.prepare('telegram', config, '555', '', media_files=files)
        first = await strict.send('telegram', config, '555', '', media_files=files, prepared_plan=plan)
        second = await strict.send('telegram', config, '555', '', media_files=files, prepared_plan=plan)
        assert first.get('ambiguous') is True and second.get('ambiguous') is True
        assert len(wire.events) == 1
        assert wire.events[0]['path'] == '/botinert/sendDocument'


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', ['slack', 'discord', 'telegram'])
async def test_strict_dispatcher_without_plan_refuses_without_dynamic_prepare(monkeypatch, platform):
    def forbidden(*args, **kwargs):
        raise AssertionError('post-claim prepare must never run')
    monkeypatch.setattr(strict, 'prepare', forbidden)
    with strict_transport_scope():
        result = await _send_to_platform(Platform(platform), PlatformConfig(token='inert'), '555', 'sealed')
    assert result == {'error': 'strict standalone prepared plan unavailable'}


@pytest.mark.asyncio
async def test_manual_standalone_telegram_keeps_canonical_sender(tmp_path, monkeypatch):
    from tools import send_message_senders as senders
    from telegram import Bot
    from aiohttp import web
    posts = []

    async def receive(request):
        data = await request.post()
        posts.append({'method': request.match_info['operation'], 'data': dict(data)})
        return web.json_response({'ok': True, 'result': {'message_id': len(posts), 'date': 1,
            'chat': {'id': 555, 'type': 'private'}, 'text': 'sealed'}})

    app = web.Application()
    app.router.add_post('/{token}/{operation}', receive)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    base = 'http://127.0.0.1:' + str(site._server.sockets[0].getsockname()[1])
    monkeypatch.setattr(senders, '_telegram_bot', lambda _token: Bot('123:inert', base_url=base + '/bot', local_mode=True))
    try:
        result = await _send_to_platform(Platform.TELEGRAM, PlatformConfig(token='inert'),
                                                '555', 'sealed', thread_id='777')
        assert result.get('success'), result
        assert len(posts) == 1 and posts[0]['method'] == 'sendMessage'
        assert posts[0]['data']['message_thread_id'] == '777'
    finally:
        await runner.cleanup()
