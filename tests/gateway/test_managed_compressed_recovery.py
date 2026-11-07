import asyncio
from contextlib import closing
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import threading

import pytest

from tests.gateway.fixtures.local_recovery_probe import Model, child_env, daemon, rpc, websocket
from tests.gateway.test_session_mutation_compress_plugin_engine import ENGINE
from tests.waiting import async_wait_until


@pytest.mark.platforms('linux')
def test_real_compressed_worker_crash_then_native_reset_never_reexecutes(tmp_path):
    root = Path(__file__).resolve().parents[2]
    home, user = tmp_path / 'state', tmp_path / 'user'
    home.mkdir()
    user.mkdir()
    plugin = home / 'plugins/recording_engine'
    plugin.mkdir(parents=True)
    (plugin / '__init__.py').write_text(ENGINE)
    peer = ThreadingHTTPServer(('127.0.0.1', 0), Model)
    peer.requests = []
    peer.blocked, peer.release = threading.Event(), threading.Event()
    thread = threading.Thread(target=peer.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{peer.server_port}/v1'
    config = {'gateway':{'multiplex_profiles':False, 'managed_workers':True},
        'model':{'provider':'custom', 'default':'managed-model', 'base_url':url},
        'context':{'engine':'recording_engine'}, 'compression':{'in_place':False},
        'auxiliary':{'title_generation':{'enabled':False}}, 'platform_toolsets':{'cli':[]}}
    (home / 'config.yaml').write_text(json.dumps(config))
    marker = tmp_path / 'finished'
    env = child_env()
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home), PYTHONPATH=str(root),
        OPENAI_API_KEY='loopback-only', OPENAI_BASE_URL=url, PYTHONUNBUFFERED='1',
        MANAGED_RECOVERY_BARRIER=str(marker), MANAGED_RECOVERY_MATCH='SEALED_COMPRESSED',
        ENGINE_LOG=str(tmp_path / 'engine.jsonl'))

    def query(sql, args=()):
        with closing(sqlite3.connect(f'file:{home / "state.db"}?mode=ro', uri=True)) as db:
            return db.execute(sql, args).fetchall()

    async def call(ws, method, **params):
        response = await rpc(ws, method, **params)
        assert 'result' in response, response
        return response['result']

    async def first(desc, owner):
        async with websocket(home, desc) as ws:
            sid = (await call(ws, 'session.create', request_id='compressed-owner', source='cli', cwd=str(home),
                model='managed-model', provider='custom', base_url=url, toolsets=[]))['session_id']
            for index in range(3):
                identity = f'warm-{index}'
                await call(ws, 'prompt.submit', session_id=sid, input_id=identity, text=identity)
                await async_wait_until(lambda:query('SELECT status FROM session_admissions WHERE request_id=?',
                    (identity,)) == [('terminal',)], timeout=35, what='warm turn settled')
            before = await call(ws, 'session.resume', session_id=sid)
            compression = await call(ws, 'session.mutate', session_id=sid, request_id='compress', operation='compress',
                payload={}, expected_revision=before['revision'], expected_generation=before['execution_generation'])
            target = compression['target_session_id']
            assert target != sid
            snapshot = await call(ws, 'session.resume', session_id=sid)
            assert 'ENGINE_SUMMARY' in json.dumps(snapshot['messages'])
            await call(ws, 'prompt.submit', session_id=sid, input_id='sealed', text='SEALED_COMPRESSED')
            await async_wait_until(marker.exists, timeout=60, what='compressed worker finished before settlement')
            [(admission,)] = query('SELECT admission_id FROM session_admissions WHERE request_id=?', ('sealed',))
            assert query('SELECT session_id,status FROM worker_executions WHERE execution_id=?',
                ('admission-worker:' + admission,)) == [(target, 'terminal')]
            before_reset = await call(ws, 'session.resume', session_id=sid)
            refused = await rpc(ws, 'session.mutate', session_id=sid, request_id='busy-reset', operation='reset', payload={},
                expected_revision=before_reset['revision'], expected_generation=before_reset['execution_generation'])
            assert 'error' in refused, refused
            assert query('SELECT status FROM session_admissions WHERE admission_id=?', (admission,)) == [('started',)]
            assert query('SELECT COUNT(*) FROM state_meta WHERE key=?', ('managed.result.v1:' + admission,)) == [(1,)]
            owner.kill()
            await asyncio.to_thread(owner.wait, 10)
            return sid, admission, target, len(peer.requests)

    async def recovered(desc, sid, admission, target):
        async with websocket(home, desc) as ws:
            receipt = await call(ws, 'prompt.receipt', session_id=sid, admission_id=admission, include_result=True)
            assert (receipt['status'], receipt['outcome']) == ('terminal', 'completed'), receipt
            assert receipt['result']['final_response'] == 'RECOVERY_ACK_SEALED_COMPRESSED'
            snapshot = await call(ws, 'session.resume', session_id=sid)
            reset = await call(ws, 'session.mutate', session_id=sid, request_id='reset-after-recovery', operation='reset', payload={},
                expected_revision=snapshot['revision'], expected_generation=snapshot['execution_generation'])
            assert reset['previous_target_session_id'] == target
            assert reset['target_session_id'] != target
            fresh = await call(ws, 'session.resume', session_id=sid)
            assert 'SEALED_COMPRESSED' not in json.dumps(fresh['messages'])
            assert (await call(ws, 'prompt.receipt', session_id=sid, admission_id=admission, include_result=True)) == receipt
            return receipt, reset

    try:
        with daemon(root, home, env, barrier=True, fixture='managed_result_daemon.py') as (owner, desc):
            sid, admission, target, count = asyncio.run(first(desc, owner))
        with daemon(root, home, env, barrier=False) as (_, desc):
            saved, reset = asyncio.run(recovered(desc, sid, admission, target))
            assert len(peer.requests) == count == 4
        async def final(desc):
            async with websocket(home, desc) as ws:
                assert (await call(ws, 'prompt.receipt', session_id=sid, admission_id=admission, include_result=True)) == saved
                snapshot = await call(ws, 'session.resume', session_id=sid)
                assert snapshot['execution_generation'] == reset['execution_generation']
                assert 'SEALED_COMPRESSED' not in json.dumps(snapshot['messages'])
        with daemon(root, home, env, barrier=False) as (_, desc):
            asyncio.run(final(desc))
            assert len(peer.requests) == count
        assert len([request for request in peer.requests if request['messages'][-1]['content'] == 'SEALED_COMPRESSED']) == 1
    finally:
        peer.shutdown()
        peer.server_close()
        thread.join(timeout=5)
