import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys

from aiohttp import web
import pytest

from cron import delivery_queue as queue, executions, jobs


@asynccontextmanager
async def peer(fault=None):
    events = []

    async def handle(request):
        body = await request.read()
        data = json.loads(body) if request.content_type == 'application/json' else dict(request.query)
        events.append({'path': request.path, 'data': data, 'body': body.hex()})
        mutation = request.path == '/chat.postMessage'
        sent = sum(e['path'] == '/chat.postMessage' for e in events)
        if mutation and fault == ('root_lost' if sent == 1 else 'body_lost'):
            request.transport.close()
            return web.Response()
        if request.path == '/users.info':
            return web.json_response({'ok': True, 'user': {'id': data.get('user'), 'is_bot': False,
                                                         'profile': {'display_name': 'Local human'}}})
        if request.path == '/conversations.info':
            return web.json_response({'ok': True, 'channel': {'id': 'CLOCAL', 'name': 'local-channel'}})
        if request.path == '/conversations.replies':
            return web.json_response({'ok': True, 'messages': [{'ts': '1700000000.000001',
                'user': 'UBOT', 'text': 'Hermes saved root'}], 'has_more': False})
        return web.json_response({'ok': True, 'channel': data.get('channel', 'CLOCAL'),
            'ts': '1700000000.000001', 'message': {'text': data.get('text', '')}})

    app = web.Application()
    app.router.add_route('*', '/{path:.*}', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    url = 'http://127.0.0.1:' + str(site._server.sockets[0].getsockname()[1])
    try:
        yield url, events
    finally:
        await runner.cleanup()


async def process(home, mode, url=None):
    env = {k: v for k, v in os.environ.items() if k in {'PATH', 'LANG', 'TZ', 'TMPDIR'}}
    env.update(HOME=str(home), USERPROFILE=str(home), HERMES_HOME=str(home), SLACK_ALLOWED_USERS='UHUMAN')
    args = [sys.executable, str(Path(__file__).with_name('strict_lifecycle_fixture.py')), str(home)]
    args.extend(['produce'] if mode == 'produce' else [url, mode])
    child = await asyncio.create_subprocess_exec(*args, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await asyncio.wait_for(child.communicate(), 50)
    receipt = {'pid': child.pid, 'mode': mode, 'exit': child.returncode,
               'stdout': stdout.decode(), 'stderr': stderr.decode()}
    print('LIFECYCLE_PROCESS ' + json.dumps(receipt))
    expected = 73 if mode == 'produce' else 88 if mode in {'create_claim', 'root_ack', 'body_claim'} else 0
    assert child.returncode == expected, receipt
    return json.loads(stdout) if stdout else receipt


def setup(home, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(queue, 'DELIVERY_DB', home / 'cron/deliveries.db')
    (home / 'scripts').mkdir()
    script = home / 'scripts/brief.py'
    script.write_text('from pathlib import Path\np=Path(' + repr(str(home / 'script-calls')) + ')\n'
        'with p.open("a") as f: f.write("executed\\n")\nprint("saved real producer brief")\n')
    job = jobs.create_job(prompt=None, script=script.name, no_agent=True, schedule='every 1h',
        deliver='origin', attach_to_session=True, origin={'platform': 'slack', 'chat_id': 'CLOCAL',
        'chat_type': 'group', 'scope_id': 'TLOCAL', 'user_id': 'UHUMAN'})
    job['execution_id'] = executions.create_execution(job['id'], source='test')['id']
    assert executions.mark_execution_handoff_pending(job['execution_id'])
    (home / 'lifecycle-request.json').write_text(json.dumps({'job': job}))
    return job


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,status', [('root_ack', 'delivered'), ('create_claim', 'unknown'), ('body_claim', 'unknown')])
async def test_real_producer_once_across_thread_crash(tmp_path, monkeypatch, mode, status):
    job = setup(tmp_path, monkeypatch)
    await process(tmp_path, 'produce')
    assert (tmp_path / 'script-calls').read_text().splitlines() == ['executed']
    assert queue.get_status(job['execution_id'])['status'] == 'pending'
    async with peer() as (url, events):
        await process(tmp_path, mode, url)
        first = await process(tmp_path, 'none', url)
        wire = list(events)
        second = await process(tmp_path, 'none', url)
    assert events == wire
    assert first['row']['status'] == second['row']['status'] == status
    assert first['script_calls'] == second['script_calls'] == ['executed']
    assert sum(e['path'] == '/chat.postMessage' and 'thread_ts' not in e['data'] for e in events) == (0 if mode == 'create_claim' else 1)
    assert bool(first['seeded_id']) is (status == 'delivered')
    assert first['seeded_id'] == second['seeded_id']
    print('LIFECYCLE_WIRE ' + json.dumps(events))


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['root_lost', 'body_lost'])
async def test_real_producer_unknown_ack_never_repeats(tmp_path, monkeypatch, fault):
    setup(tmp_path, monkeypatch)
    await process(tmp_path, 'produce')
    async with peer(fault) as (url, events):
        one = await process(tmp_path, 'none', url)
        two = await process(tmp_path, 'none', url)
    assert one['script_calls'] == two['script_calls'] == ['executed']
    assert one['row']['status'] == two['row']['status'] == 'unknown'
    assert one['seeded_id'] is two['seeded_id'] is None
    assert len(events) == (1 if fault == 'root_lost' else 2)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['inbound', 'unauthorized', 'strict_mention', 'inactive', 'ended', 'reset', 'suspended'])
async def test_fresh_slack_inbound_auth_wake_route_and_transcript(tmp_path, monkeypatch, mode):
    setup(tmp_path, monkeypatch)
    await process(tmp_path, 'produce')
    async with peer() as (url, events):
        seeded = await process(tmp_path, 'none', url)
        inbound = await process(tmp_path, mode, url)
    assert inbound['script_calls'] == ['executed']
    assert seeded['seeded_id'] == inbound['seeded_id']
    assert len([e for e in events if e['path'] == '/chat.postMessage']) == 2
    assert inbound['auth'] and inbound['auth'][0]['thread'] == '1700000000.000001'
    if mode in {'unauthorized', 'strict_mention'}:
        assert inbound['routes'] == []
        assert inbound['auth'][0]['allowed'] is (mode != 'unauthorized')
        return
    assert len(inbound['routes']) == 1
    route = inbound['routes'][0]
    assert route['source']['scope_id'] == 'TLOCAL' and route['source']['thread_id'] == '1700000000.000001'
    assert route['key'].endswith(':slack:group:TLOCAL:CLOCAL:1700000000.000001')
    if mode in {'ended', 'reset', 'suspended'}:
        assert route['session_id'] != seeded['seeded_id']
        assert not any('saved real producer brief' in r.get('content', '') for r in route['transcript'])
    else:
        assert route['session_id'] == seeded['seeded_id']
        assert sum('saved real producer brief' in r.get('content', '') for r in route['transcript']) == 1
