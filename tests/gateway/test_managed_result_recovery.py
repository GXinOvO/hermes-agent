"""Consume a real finished worker after SIGKILL, without another model request."""
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
from tests.waiting import async_wait_until


@pytest.mark.platforms("linux")
def test_sealed_worker_result_survives_real_owner_kill(tmp_path):
    root = Path(__file__).resolve().parents[2]
    home, user = tmp_path / 'state', tmp_path / 'user'
    home.mkdir()
    user.mkdir()
    peer = ThreadingHTTPServer(('127.0.0.1', 0), Model)
    peer.requests = []
    peer.blocked, peer.release = threading.Event(), threading.Event()
    thread = threading.Thread(target=peer.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{peer.server_port}/v1'
    (home / 'config.yaml').write_text(json.dumps({
        'gateway': {'multiplex_profiles': False, 'managed_workers': True},
        'model': {'provider': 'custom', 'default': 'managed-model', 'base_url': url},
        'auxiliary': {'title_generation': {'enabled': False}}, 'platform_toolsets': {'cli': []}}))
    marker = tmp_path / 'finished'
    env = child_env()
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home), PYTHONPATH=str(root),
               OPENAI_API_KEY='loopback-only', OPENAI_BASE_URL=url, PYTHONUNBUFFERED='1',
               MANAGED_RECOVERY_BARRIER=str(marker))

    def query(sql, args=()):
        with closing(sqlite3.connect(f'file:{home / "state.db"}?mode=ro', uri=True)) as db:
            return db.execute(sql, args).fetchall()

    async def first(desc, owner):
        async with websocket(home, desc) as ws:
            created = await rpc(ws, 'session.create', request_id='managed', source='cli', cwd=str(home),
                                model='managed-model', provider='custom', base_url=url,
                                api_key='loopback-only', toolsets=[], ignore_rules=True)
            assert 'result' in created, created
            sid = created['result']['session_id']
            reply = await rpc(ws, 'prompt.submit', session_id=sid, input_id='finished', text='SEALED_RESULT')
            assert 'result' in reply, reply
            await async_wait_until(marker.exists, timeout=90, what='worker finished before settlement')
            [(admission, status)] = query('SELECT admission_id,status FROM session_admissions WHERE request_id=?', ('finished',))
            assert status == 'started'
            assert query('SELECT status FROM worker_executions WHERE execution_id=?', ('admission-worker:' + admission,)) == [('terminal',)]
            owner.kill()
            await asyncio.to_thread(owner.wait, 10)
            counters = query('SELECT SUM(input_tokens),SUM(output_tokens),SUM(api_call_count) FROM sessions')
            return sid, admission, len(peer.requests), counters

    async def recovered(desc, sid, admission):
        async with websocket(home, desc) as ws:
            result = await rpc(ws, 'prompt.receipt', session_id=sid, admission_id=admission, include_result=True)
            assert result.get('result', {}).get('status') == 'terminal', result
            assert result['result']['outcome'] == 'completed', result
            assert result['result']['result']['final_response'] == 'RECOVERY_ACK_SEALED_RESULT', result
            return result['result']['result']

    try:
        with daemon(root, home, env, barrier=True, fixture='managed_result_daemon.py') as (owner, desc):
            sid, admission, calls, counters = asyncio.run(first(desc, owner))
            old_epoch = desc['authority_epoch']
        with daemon(root, home, env, barrier=False) as (_, desc):
            assert desc['authority_epoch'] > old_epoch
            saved = asyncio.run(recovered(desc, sid, admission))
            assert len(peer.requests) == calls == 1
        with daemon(root, home, env, barrier=False) as (_, desc):
            assert asyncio.run(recovered(desc, sid, admission)) == saved
            assert len(peer.requests) == calls
            assert query('SELECT SUM(input_tokens),SUM(output_tokens),SUM(api_call_count) FROM sessions') == counters
    finally:
        peer.shutdown()
        peer.server_close()
        thread.join(timeout=5)
