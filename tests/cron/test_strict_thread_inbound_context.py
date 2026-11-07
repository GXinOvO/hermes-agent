from contextlib import asynccontextmanager
import json

from aiohttp import web
import pytest

from tests.cron.test_strict_thread_lifecycle import process, setup


@asynccontextmanager
async def history_peer():
    messages, requests = [], []

    async def handle(request):
        raw = await request.read()
        data = json.loads(raw) if request.content_type == 'application/json' else dict(request.query)
        requests.append({'path': request.path, 'data': data})
        if request.path == '/chat.postMessage':
            ts = '1700000000.' + str(len(messages) + 1).zfill(6)
            messages.append({'ts': ts, 'user': 'UBOT', 'bot_id': 'BLOCAL',
                             'text': data['text'], 'thread_ts': data.get('thread_ts')})
            return web.json_response({'ok': True, 'channel': data['channel'], 'ts': ts,
                                      'message': {'text': data['text']}})
        if request.path == '/conversations.replies':
            return web.json_response({'ok': True, 'messages': list(messages), 'has_more': False})
        if request.path == '/users.info':
            return web.json_response({'ok': True, 'user': {'id': data.get('user'),
                'is_bot': data.get('user') == 'UBOT', 'profile': {'display_name': data.get('user')}}})
        if request.path == '/conversations.info':
            return web.json_response({'ok': True, 'channel': {'id': 'CLOCAL', 'name': 'local-channel'}})
        if request.path == '/agents.sessions.setStatus':
            return web.json_response({'ok': True})
        raise AssertionError('unexpected local SDK method ' + request.path)

    app = web.Application()
    app.router.add_route('*', '/{path:.*}', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    try:
        yield 'http://127.0.0.1:' + str(site._server.sockets[0].getsockname()[1]), messages, requests
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['inbound', 'inactive', 'ended', 'reset', 'suspended', 'ended_mention', 'reset_mention'])
async def test_thread_history_context_is_separate_from_seed_transcript(tmp_path, monkeypatch, mode):
    setup(tmp_path, monkeypatch)
    await process(tmp_path, 'produce')
    async with history_peer() as (url, messages, requests):
        seeded = await process(tmp_path, 'none', url)
        inbound = await process(tmp_path, mode, url)
    assert len(messages) == 2 and messages[1]['text'] == 'saved real producer brief'
    assert len(inbound['routes']) == 1 and inbound['script_calls'] == ['executed']
    route = inbound['routes'][0]
    prior_in_transcript = any('saved real producer brief' in row.get('content', '') for row in route['transcript'])
    prior_in_context = 'saved real producer brief' in (route['channel_context'] or '')
    prior_in_input = 'saved real producer brief' in route['assembled_text']
    if mode in {'inbound', 'inactive'}:
        assert route['session_id'] == seeded['seeded_id'] and prior_in_transcript
    else:
        assert route['session_id'] != seeded['seeded_id'] and not prior_in_transcript
    if prior_in_context:
        assert prior_in_input and '[New message]' in route['assembled_text']
    if mode in {'suspended', 'ended_mention', 'reset_mention'}:
        assert prior_in_context and prior_in_input
        assert any(item['path'] == '/conversations.replies' for item in requests)
    else:
        assert not prior_in_context and not prior_in_input
    print('ACTUAL_HISTORY_CONTEXT ' + json.dumps({'mode': mode, 'session_id': route['session_id'],
        'seeded_id': seeded['seeded_id'], 'prior_in_transcript': prior_in_transcript,
        'prior_in_context': prior_in_context, 'prior_in_assembled_input': prior_in_input,
        'requests': requests, 'channel_context': route['channel_context'], 'assembled_text': route['assembled_text']}))
