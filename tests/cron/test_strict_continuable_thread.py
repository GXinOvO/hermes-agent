"""Native public queue / Slack SDK HTTP evidence for staged continuable threads."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys

from aiohttp import web
import pytest

from cron import delivery_queue as queue


@asynccontextmanager
async def peer(fault=None, held=None, race=False):
    events = []
    arrivals = []
    ready = asyncio.Event()

    async def handle(request):
        if race and request.path == '/claim-gate':
            arrivals.append(1)
            if len(arrivals) == 2:
                ready.set()
            await ready.wait()
            return web.Response(body=b'go')
        body = await request.read()
        data = json.loads(body) if request.content_type == 'application/json' else dict(request.query)
        events.append({'path': request.path, 'data': data, 'body': body.hex(),
                       'authorization': request.headers.get('Authorization', '')})
        if held is not None and len(events) == 1:
            held[0].set()
            await held[1].wait()
        if fault == ('root_lost' if len(events) == 1 else 'body_lost'):
            request.transport.close()
            return web.Response()
        method = request.path.rsplit('/', 1)[-1]
        if method == 'files.getUploadURLExternal':
            return web.json_response({'ok': True, 'file_id': 'FLOCAL', 'upload_url': base + '/upload'})
        if method == 'upload':
            return web.Response(text='OK')
        if method == 'files.completeUploadExternal':
            return web.json_response({'ok': True, 'files': [{'id': 'FLOCAL'}]})
        return web.json_response({'ok': True, 'channel': data.get('channel', 'CLOCAL'), 'ts': '1700000000.000001',
                                  'message': {'text': data.get('text', '')}})

    app = web.Application()
    app.router.add_route('*', '/{path:.*}', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    base = 'http://127.0.0.1:' + str(site._server.sockets[0].getsockname()[1])
    try:
        yield base, events
    finally:
        await runner.cleanup()


async def owner(home, url, stop='none'):
    env = {k: v for k, v in os.environ.items() if k in {'PATH', 'LANG', 'TZ', 'TMPDIR'}}
    env.update(HOME=str(home), USERPROFILE=str(home), HERMES_HOME=str(home))
    proc = await asyncio.create_subprocess_exec(sys.executable, str(Path(__file__).with_name('strict_thread_fixture.py')),
        str(home), url, stop, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await asyncio.wait_for(proc.communicate(), 40)
    print('FRESH_OWNER ' + json.dumps({'pid': proc.pid, 'exit': proc.returncode,
                                      'stdout': stdout.decode(), 'stderr': stderr.decode()}))
    assert proc.returncode == (88 if stop in {'before_claim', 'create_claim', 'root_ack', 'body_claim'} else 0), stderr.decode()
    return json.loads(stdout) if stdout else None


def enqueue(home, monkeypatch, content='sealed saved result'):
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(queue, 'DELIVERY_DB', home / 'cron' / 'deliveries.db')
    (home / 'runner-count').write_text('0')
    job = {'id': 'thread-job', 'execution_id': 'thread-exec', 'deliver': 'origin',
           'origin': {'platform': 'slack', 'chat_id': 'CLOCAL', 'chat_type': 'group',
                      'scope_id': 'TLOCAL', 'user_id': 'UHUMAN'}}
    queue.enqueue('thread-exec', job, content)
    return job


@pytest.mark.asyncio
async def test_public_native_queue_creates_continuable_thread(tmp_path, monkeypatch):
    enqueue(tmp_path, monkeypatch)
    async with peer() as (url, events):
        first = await owner(tmp_path, url)
        second = await owner(tmp_path, url)
    print('THREAD_WIRE ' + json.dumps(events))
    assert first['processed'] == 1
    assert second['processed'] == 0
    assert queue.get_status('thread-exec')['status'] == 'delivered'
    assert len(events) == 2
    assert events[0]['data']['channel'] == 'CLOCAL' and 'thread_ts' not in events[0]['data']
    assert events[1]['data']['thread_ts'] == '1700000000.000001'
    assert events[1]['data']['text'] == 'sealed saved result'
    assert first['session_id'] == second['session_id'] and second['session_id']
    assert any('sealed saved result' in row['content'] for row in second['transcript'])
    assert first['runner_count'] == second['runner_count'] == '0'


@pytest.mark.asyncio
@pytest.mark.parametrize('stop,roots,status', [('before_claim', 0, 'delivered'), ('create_claim', 0, 'unknown'),
                                            ('root_ack', 1, 'delivered'), ('body_claim', 1, 'unknown')])
async def test_native_crash_stages_freshowners_only_consume(tmp_path, monkeypatch, stop, roots, status):
    content = 'saved crash result'
    if stop == 'root_ack':
        path = tmp_path / 'brief.txt'
        path.write_bytes(b'sealed attachment bytes')
        content += '\nMEDIA: ' + str(path)
    enqueue(tmp_path, monkeypatch, content)
    async with peer() as (url, events):
        await owner(tmp_path, url, stop)
        row = queue.get_status('thread-exec')
        print('CRASH_SQLITE ' + json.dumps(row))
        assert len(events) == roots
        if stop == 'root_ack':
            staged = json.loads(row['thread_json'])
            assert staged['items'][0]['stage'] == 'root_acked'
            assert staged['items'][0]['receipt']['ts'] == '1700000000.000001'
        one = await owner(tmp_path, url)
        count = len(events)
        two = await owner(tmp_path, url)
    assert queue.get_status('thread-exec')['status'] == status
    assert len(events) == count
    assert one['runner_count'] == two['runner_count'] == '0'
    assert sum(e['path'] == '/chat.postMessage' and 'thread_ts' not in e['data'] for e in events) == int(status == 'delivered' or roots > 0)
    if status == 'unknown':
        assert one['session_id'] is None and two['session_id'] is None
    else:
        assert one['session_id'] == two['session_id'] and two['session_id']
        assert any('saved crash result' in r['content'] for r in two['transcript'])
    if stop == 'root_ack':
        assert [e['path'] for e in events] == ['/chat.postMessage', '/chat.postMessage',
            '/files.getUploadURLExternal', '/upload', '/files.completeUploadExternal']
        complete = events[-1]['data']
        assert complete.get('thread_ts') == '1700000000.000001'
        assert bytes.fromhex(events[-2]['body']) == b'sealed attachment bytes'
    print('CRASH_WIRE ' + json.dumps({'stage': stop, 'events': events}))


@pytest.mark.asyncio
@pytest.mark.parametrize('fault,total', [('root_lost', 1), ('body_lost', 2)])
async def test_socket_accepted_unknown_never_recreates_or_seeds(tmp_path, monkeypatch, fault, total):
    enqueue(tmp_path, monkeypatch)
    async with peer(fault) as (url, events):
        first = await owner(tmp_path, url)
        second = await owner(tmp_path, url)
    assert len(events) == total
    assert first['row']['status'] == second['row']['status'] == 'unknown'
    assert first['session_id'] is second['session_id'] is None
    assert first['runner_count'] == second['runner_count'] == '0'
    print('UNKNOWN_WIRE ' + json.dumps(events))


@pytest.mark.asyncio
async def test_concurrent_readers_protect_live_creator(tmp_path, monkeypatch):
    enqueue(tmp_path, monkeypatch)
    held = (asyncio.Event(), asyncio.Event())
    async with peer(held=held) as (url, events):
        task = asyncio.create_task(owner(tmp_path, url))
        try:
            await asyncio.wait_for(held[0].wait(), 20)
            other = await owner(tmp_path, url)
            assert other['processed'] == 0
            assert other['row']['status'] == 'delivering'
            assert len(events) == 1
        finally:
            held[1].set()
        first = await task
        second = await owner(tmp_path, url)
    assert first['processed'] == 1 and second['processed'] == 0
    assert len(events) == 2


@pytest.mark.asyncio
async def test_two_processes_race_actual_claim_boundary(tmp_path, monkeypatch):
    enqueue(tmp_path, monkeypatch)
    async with peer(race=True) as (url, events):
        results = await asyncio.gather(owner(tmp_path, url, 'race'), owner(tmp_path, url, 'race'))
        again = await owner(tmp_path, url)
    assert sorted(result['processed'] for result in results) == [0, 1]
    assert again['processed'] == 0 and again['row']['status'] == 'delivered'
    assert len(events) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('drift', ['receipt_ts', 'receipt_ack', 'receipt_type', 'stage_type', 'target_type',
                                  'home', 'content', 'token_route'])
async def test_root_receipt_corruption_or_plan_drift_never_recreates(tmp_path, monkeypatch, drift):
    enqueue(tmp_path, monkeypatch)
    async with peer() as (url, events):
        await owner(tmp_path, url, 'root_ack')
        row = queue.get_status('thread-exec')
        saved = json.loads(row['thread_json'])
        changes = {
            'receipt_ts': lambda: saved['items'][0]['receipt'].update(ts='1700000099.000001'),
            'receipt_ack': lambda: saved['items'][0]['receipt'].update(ok=False),
            'receipt_type': lambda: saved['items'][0].update(receipt='1700000000.000001'),
            'stage_type': lambda: saved['items'][0].update(stage=[]),
            'target_type': lambda: saved['binding']['targets'][0].update(root=[]),
            'home': lambda: saved['binding'].update(home=str(tmp_path / 'other-profile')),
            'content': lambda: None,
            'token_route': lambda: saved['binding']['targets'][0].update(transport_sha256='0' * 64),
        }
        changes[drift]()
        if drift in {'target_type', 'home', 'token_route'}:
            from cron.strict_thread_delivery import digest
            saved['binding_sha256'] = digest(saved['binding'])
        with queue._transaction() as conn:
            conn.execute('UPDATE deliveries SET thread_json=?, content=? WHERE execution_id=?',
                         (json.dumps(saved), 'altered body' if drift == 'content' else row['content'], 'thread-exec'))
        queue.enqueue('sibling-exec', {'id': 'sibling-job', 'execution_id': 'sibling-exec', 'deliver': 'local'}, 'inert sibling')
        first = await owner(tmp_path, url)
        second = await owner(tmp_path, url)
    assert len(events) == 1
    assert first['session_id'] is second['session_id'] is None
    assert first['row']['status'] in {'pending', 'delivering'}
    assert queue.get_status('sibling-exec')['status'] == 'suppressed'