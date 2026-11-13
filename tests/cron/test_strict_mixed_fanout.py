"""Public queue mixed fanout across fresh processes and real loopback SDK wire."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys

from aiohttp import web
import pytest

from cron import delivery_queue as queue


_native = []


@pytest.fixture(autouse=True)
def native_receipt(record_property):
    _native.clear()
    yield
    record_property('fanout_native', json.dumps(_native, sort_keys=True))


@asynccontextmanager
async def peer(lost=None, hold=None, race=False):
    events, arrivals = [], []
    ready = asyncio.Event()

    async def receive(request):
        if request.path == '/claim-gate' and race:
            arrivals.append(1)
            if len(arrivals) == 2:
                ready.set()
            await ready.wait()
            return web.Response(body=b'go')
        body = await request.read()
        if request.content_type == 'application/json':
            data = json.loads(body)
        elif request.content_type == 'application/x-www-form-urlencoded':
            from urllib.parse import parse_qsl
            data = dict(parse_qsl(body.decode()))
        else:
            data = dict(request.query)
        events.append({'path': request.path, 'data': data, 'body': body.hex(),
                       'authorization': request.headers.get('Authorization', '')})
        if hold and len(events) == hold[0]:
            hold[1].set()
            await hold[2].wait()
        if len(events) == lost:
            request.transport.close()
            return web.Response()
        if request.path.endswith('getUploadURLExternal'):
            return web.json_response({'ok': True, 'file_id': 'FLOCAL', 'upload_url': base + '/upload'})
        if request.path == '/upload':
            return web.Response(text='OK')
        if request.path.endswith('completeUploadExternal'):
            return web.json_response({'ok': True, 'files': [{'id': 'FLOCAL'}]})
        if '/bot' in request.path:
            return web.json_response({'ok': True, 'result': {'message_id': len(events), 'date': 1,
                'chat': {'id': 555, 'type': 'private'}, 'text': 'ACK'}})
        return web.json_response({'ok': True, 'channel': data.get('channel'),
            'ts': '1700000000.' + str(len(events)).zfill(6), 'message': {'text': data.get('text', '')}})

    app = web.Application()
    app.router.add_route('*', '/{path:.*}', receive)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    base = 'http://127.0.0.1:' + str(site._server.sockets[0].getsockname()[1])
    try:
        yield base, events
    finally:
        _native.append({'wire': events})
        print('FANOUT_WIRE ' + json.dumps(events))
        await runner.cleanup()


async def owner(home, url, stop='none'):
    env = {key: value for key, value in os.environ.items() if key in {'PATH', 'TZ', 'LANG'}}
    scratch = home / 'child-tmp'
    scratch.mkdir(exist_ok=True)
    child_python = os.environ.get('HERMES_PYTHON') or sys.executable
    env.update(HOME=str(home), USERPROFILE=str(home), HERMES_HOME=str(home), TMPDIR=str(scratch),
               HERMES_PYTHON=child_python)
    proc = await asyncio.create_subprocess_exec(child_python,
        str(Path(__file__).with_name('strict_fanout_fixture.py')), str(home), url, stop,
        env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await asyncio.wait_for(proc.communicate(), 45)
    _native.append({'pid': proc.pid, 'exit': proc.returncode,
        'stdout': stdout.decode(), 'stderr': stderr.decode()})
    print('FANOUT_OWNER ' + json.dumps({'pid': proc.pid, 'exit': proc.returncode,
        'stdout': stdout.decode(), 'stderr': stderr.decode()}))
    assert proc.returncode == (0 if stop in {'none', 'race', 'mutate'} else 88), stderr.decode()
    return json.loads(stdout) if stdout else None


def enqueue(home, monkeypatch, deliver='origin,slack:C12345678:1700000777.000001', content='sealed mixed result', **settings):
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(queue, 'DELIVERY_DB', home / 'cron' / 'deliveries.db')
    (home / 'fanout-settings.json').write_text(json.dumps(settings))
    job = {'id': 'fanout-job', 'execution_id': 'fanout-exec', 'deliver': deliver,
           'origin': {'platform': 'slack', 'chat_id': 'CLOCAL',
                      'scope_id': 'TLOCAL', 'user_id': 'UHUMAN'}}
    queue.enqueue('fanout-exec', job, content)
    return job


@pytest.mark.asyncio
async def test_mixed_new_thread_and_existing_thread(tmp_path, monkeypatch):
    enqueue(tmp_path, monkeypatch)
    async with peer() as (url, events):
        first = await owner(tmp_path, url)
        again = await owner(tmp_path, url)
    assert first['processed'] == 1 and again['processed'] == 0
    assert again['row']['status'] == 'delivered'
    assert [(event['data']['channel'], event['data'].get('thread_ts')) for event in events] == [
        ('CLOCAL', None), ('CLOCAL', '1700000000.000001'), ('C12345678', '1700000777.000001')]
    assert all(event['authorization'] == 'Bearer inert-fanout' for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize('stop,count,status', [
    ('after_claim', 0, 'delivered'), ('after:0:create_claimed', 0, 'unknown'),
    ('after:0:root_acked', 1, 'delivered'), ('after:0:body_claimed', 1, 'unknown'),
    ('after:0:done', 2, 'delivered'), ('before:1:target_claimed', 2, 'delivered'),
    ('after:1:target_claimed', 2, 'unknown'), ('before:1:done', 3, 'unknown'),
    ('after:1:done', 3, 'delivered')])
async def test_mixed_crashes_preserve_done_prefix(tmp_path, monkeypatch, stop, count, status):
    enqueue(tmp_path, monkeypatch, deliver='origin,slack:C12345678:1700000777.000001,telegram:555', cold_telegram=True)
    async with peer() as (url, events):
        await owner(tmp_path, url, stop)
        row = queue.get_status('fanout-exec')
        saved = json.loads(row['thread_json'])
        _native.append({'saved_stage': saved})
        print('FANOUT_STAGE ' + json.dumps(saved))
        assert saved['binding']['version'] == 2 and len(saved['items']) == 3
        assert len(events) == count
        one = await owner(tmp_path, url)
        completed_count = len(events)
        two = await owner(tmp_path, url)
        assert len(events) == completed_count
    assert one['row']['status'] == two['row']['status'] == status
    assert len(events) == (4 if status == 'delivered' else count)
    if status == 'delivered':
        assert [e['data'].get('thread_ts') for e in events[:3]] == [None, '1700000000.000001', '1700000777.000001']
        assert events[-1]['path'].endswith('/sendMessage')
    if count < 2 and status == 'unknown':
        assert not one['sessions'] and not two['sessions']


@pytest.mark.asyncio
@pytest.mark.parametrize('lost', [1, 2, 3, 4])
async def test_mixed_accepted_lost_ack_stops_all_later_targets(tmp_path, monkeypatch, lost):
    enqueue(tmp_path, monkeypatch, deliver='origin,slack:C12345678:1700000777.000001,telegram:555', cold_telegram=True)
    async with peer(lost=lost) as (url, events):
        one = await owner(tmp_path, url)
        two = await owner(tmp_path, url)
        assert len(events) == lost
    assert one['row']['status'] == two['row']['status'] == 'unknown'
    if lost < 3:
        assert not two['sessions']


@pytest.mark.asyncio
async def test_multiple_new_threads_have_distinct_roots(tmp_path, monkeypatch):
    job = enqueue(tmp_path, monkeypatch, deliver='origin,slack:CSECOND')
    job['attach_to_session'] = True
    with queue._transaction() as conn:
        conn.execute('UPDATE deliveries SET job_json=? WHERE execution_id=?',
            (json.dumps(job, ensure_ascii=False, sort_keys=True), 'fanout-exec'))
    async with peer() as (url, events):
        await owner(tmp_path, url, 'after:0:done')
        first = await owner(tmp_path, url)
        await owner(tmp_path, url)
    assert first['row']['status'] == 'delivered' and len(events) == 4
    assert [e['data'].get('thread_ts') for e in events] == [None, '1700000000.000001', None, '1700000000.000003']
    assert len(first['sessions']) == 2


@pytest.mark.asyncio
async def test_mixed_native_flat_and_cold_targets(tmp_path, monkeypatch):
    job = enqueue(tmp_path, monkeypatch,
        deliver='origin,slack:C12345678:1700000777.000001,slack:DFLAT,telegram:555', native_telegram=True)
    job['attach_to_session'] = True
    with queue._transaction() as conn:
        conn.execute('UPDATE deliveries SET job_json=? WHERE execution_id=?',
            (json.dumps(job, ensure_ascii=False, sort_keys=True), 'fanout-exec'))
    async with peer() as (url, events):
        one = await owner(tmp_path, url)
        await owner(tmp_path, url)
    assert one['row']['status'] == 'delivered' and len(events) == 5
    assert events[3]['data']['channel'] == 'DFLAT' and 'thread_ts' not in events[3]['data']
    assert events[-1]['path'].endswith('/sendMessage')
    assert any('DFLAT' in key and value['transcript'] for key, value in one['sessions'].items())


@pytest.mark.asyncio
@pytest.mark.parametrize('lost', [None, 3])
async def test_concurrent_fresh_readers_do_not_steal_live_target(tmp_path, monkeypatch, lost):
    enqueue(tmp_path, monkeypatch)
    held = (3, asyncio.Event(), asyncio.Event())
    async with peer(lost=lost, hold=held) as (url, events):
        task = asyncio.create_task(owner(tmp_path, url))
        try:
            await asyncio.wait_for(held[1].wait(), 20)
            other = await owner(tmp_path, url)
            assert other['processed'] == 0 and other['row']['status'] == 'delivering'
            assert len(events) == 3
        finally:
            held[2].set()
        one = await task
        await owner(tmp_path, url)
    assert one['row']['status'] == ('delivered' if lost is None else 'unknown')
    assert len(events) == 3


@pytest.mark.asyncio
async def test_racing_fresh_readers_claim_one_mixed_plan(tmp_path, monkeypatch):
    enqueue(tmp_path, monkeypatch)
    async with peer(race=True) as (url, events):
        values = await asyncio.gather(owner(tmp_path, url, 'race'), owner(tmp_path, url, 'race'))
        await owner(tmp_path, url)
    assert sorted(v['processed'] for v in values) == [0, 1] and len(events) == 3


@pytest.mark.asyncio
async def test_invalid_later_target_has_zero_claims_and_submissions(tmp_path, monkeypatch):
    enqueue(tmp_path, monkeypatch, deliver='origin,slack:C12345678:1700000777.000001,discord:123')
    async with peer() as (url, events):
        one = await owner(tmp_path, url)
    assert one['processed'] == 0 and one['row']['status'] == 'pending'
    assert one['row']['owner_pid'] is None and one['row']['thread_json'] is None and events == []


@pytest.mark.asyncio
async def test_mixed_claim_consumes_frozen_media_and_transport(tmp_path, monkeypatch):
    path = tmp_path / 'sealed.txt'
    path.write_bytes(b'A sealed fanout')
    enqueue(tmp_path, monkeypatch, content='saved\nMEDIA: ' + str(path))
    async with peer() as (url, events):
        one = await owner(tmp_path, url, 'mutate')
    assert one['row']['status'] == 'delivered' and path.read_bytes() == b'B after claim'
    uploads = [e for e in events if e['path'] == '/upload']
    assert len(uploads) == 2 and all(bytes.fromhex(e['body']) == b'A sealed fanout' for e in uploads)
    assert all(e['authorization'] == 'Bearer inert-fanout' for e in events if e['path'] != '/upload')


@pytest.mark.asyncio
@pytest.mark.parametrize('stop', ['after:0:done', 'after:1:done'])
async def test_mixed_local_and_cold_slack_recover_prefix(tmp_path, monkeypatch, stop):
    cold = tmp_path / 'profiles' / 'cold'
    cold.mkdir(parents=True)
    (cold / 'config.yaml').write_text('platforms:\n  slack:\n    enabled: true\n    token: inert-cold-fanout\n')
    enqueue(tmp_path, monkeypatch, deliver='origin,local:LOCAL_TARGET,slack:CCOLD', local=True, cold_slack=True)
    async with peer() as (url, events):
        await owner(tmp_path, url, stop)
        one = await owner(tmp_path, url)
        two = await owner(tmp_path, url)
    assert one['row']['status'] == two['row']['status'] == 'delivered'
    assert len(events) == 3 and events[-1]['data']['channel'] == 'CCOLD'
    assert events[-1]['authorization'] == 'Bearer inert-cold-fanout'
    assert sum('sealed mixed result' in str(m.get('content')) for m in two['local_messages']) == 1


@pytest.mark.asyncio
async def test_mixed_preflight_media_aba_before_claim(tmp_path, monkeypatch):
    from cron import strict_delivery as strict

    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.session import SessionStore
    from plugins.platforms.slack.adapter import SlackAdapter
    from slack_sdk.web.async_client import AsyncWebClient
    from types import SimpleNamespace
    file = tmp_path / 'aba.txt'
    file.write_bytes(b'A')
    enqueue(tmp_path, monkeypatch, content='saved\nMEDIA: ' + str(file))
    pc = PlatformConfig(enabled=True, token='inert')
    config = GatewayConfig(platforms={Platform.SLACK: pc})
    monkeypatch.setattr('gateway.config.load_gateway_config', lambda: config)
    monkeypatch.setattr('cron.scheduler.load_config', lambda: {'cron': {'wrap_response': False, 'mirror_delivery': True}})
    adapter = SlackAdapter(pc)
    adapter._app = SimpleNamespace(client=AsyncWebClient(token='inert', base_url='http://127.0.0.1:1/', proxy=''))
    adapter._team_clients = {'TLOCAL': adapter._app.client}
    adapter._channel_team['C12345678'] = 'TLOCAL'
    adapter._session_store = SessionStore(tmp_path / 'sessions', config)
    original = Path.read_bytes

    def aba(path):
        if path == file:
            file.write_bytes(b'B')
            frozen = original(path)
            file.write_bytes(b'A')
            return frozen
        return original(path)

    monkeypatch.setattr(Path, 'read_bytes', aba)
    job = json.loads(queue.get_status('fanout-exec')['job_json'])
    with pytest.raises(RuntimeError, match='frozen media.*differ'):
        await asyncio.to_thread(strict.prepare, job, queue.get_status('fanout-exec')['content'], False, adapters={Platform.SLACK: adapter},
            loop=asyncio.get_running_loop())
    assert queue.get_status('fanout-exec')['owner_pid'] is None and original(file) == b'A'


@pytest.mark.asyncio
async def test_all_target_kinds_in_one_ordered_fanout(tmp_path, monkeypatch):
    enqueue(tmp_path, monkeypatch, deliver='origin,slack:C12345678:1700000777.000001,slack:DFLAT,local:LOCAL_TARGET,slack:CCOLD,telegram:555',
        local=True, cold_slack=True, native_telegram=True)
    async with peer() as (url, events):
        await owner(tmp_path, url, 'after:3:done')
        saved = json.loads(queue.get_status('fanout-exec')['thread_json'])
        _native.append({'saved_stage': saved})
        assert [item['stage'] for item in saved['items']] == ['done'] * 4 + ['initial'] * 2
        one = await owner(tmp_path, url)
        two = await owner(tmp_path, url)
    assert one['row']['status'] == two['row']['status'] == 'delivered'
    assert len(events) == 6
    assert [event['data'].get('channel') for event in events[:5]] == ['CLOCAL', 'CLOCAL', 'C12345678', 'DFLAT', 'CCOLD']
    assert events[4]['authorization'] == 'Bearer inert-cold-fanout'
    assert events[5]['path'].endswith('/sendMessage')
    assert sum('sealed mixed result' in str(m.get('content')) for m in two['local_messages']) == 1
