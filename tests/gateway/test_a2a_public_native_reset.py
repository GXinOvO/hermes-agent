"""Ordinary public HUMAN results survive turns but follow native target boundaries."""
import asyncio
from contextlib import closing
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sqlite3
import threading

import pytest

from gateway.config import GatewayConfig
from gateway.session import SessionStore
from plugins.platforms.a2a import durable_tasks, protocol
from tests.gateway.a2a_durable_peer import rpc
from tests.gateway.fixtures.local_recovery_probe import Model, child_env, daemon, websocket
from tests.gateway.fixtures.local_recovery_probe import rpc as control_rpc
from tests.gateway.test_default_public_a2a_startup import read_state, send
from tests.gateway.test_native_telegram_startup_recovery import wait_for


def raw_task(home, task):
    with closing(sqlite3.connect(f'file:{home / "state.db"}?mode=ro', uri=True)) as db:
        return db.execute('SELECT value FROM state_meta WHERE key=?', (durable_tasks._key(task),)).fetchone()[0]


async def native_mutate(home, desc, value, operation):
    sid = value['binding']['session_id']
    with closing(sqlite3.connect(f'file:{home / "state.db"}?mode=ro', uri=True)) as db:
        revision, generation = db.execute('SELECT runtime_revision,runtime_generation FROM sessions WHERE id=?', (sid,)).fetchone()
        payload = {}
        if operation == 'rewind':
            payload['target_message_id'] = db.execute("SELECT id FROM messages WHERE session_id=? AND role='user' AND active=1 ORDER BY id LIMIT 1", (sid,)).fetchone()[0]
    async with websocket(home, desc) as ws:
        result = await control_rpc(ws, 'session.mutate', session_id=sid, request_id='public-' + operation,
            expected_revision=revision, expected_generation=generation, operation=operation, payload=payload)
    assert 'result' in result, result
    return result


@pytest.mark.parametrize('operation', ['unchanged', 'next-turn', 'delete', 'rewind', 'session-store-reset', 'reset-resume',
    'missing-seal', 'malformed-seal', 'changed-seal-target', 'deleted-missing-seal'])
def test_native_human_a2a_result_target_lifecycle(tmp_path, monkeypatch, operation):
    root = Path(__file__).resolve().parents[2]
    home, user = tmp_path / 'state', tmp_path / 'user'
    home.mkdir(mode=0o700)
    user.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HOME', str(user))
    model = ThreadingHTTPServer(('127.0.0.1', 0), Model)
    model.requests, model.blocked, model.release = [], threading.Event(), threading.Event()
    thread = threading.Thread(target=model.serve_forever, daemon=True)
    thread.start()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    base = f'http://127.0.0.1:{model.server_port}/v1'
    config = {'model': {'provider': 'custom', 'default': 'local-wire-stub', 'base_url': base},
        'gateway': {'multiplex_profiles': False},
        'platforms': {'a2a': {'enabled': True, 'extra': {'port': port}}},
        'auxiliary': {'title_generation': {'enabled': False}}, 'platform_toolsets': {'a2a': []},
        'terminal': {'cwd': str(home)}}
    (home / 'config.yaml').write_text(json.dumps(config))
    env = child_env()
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home), PYTHONPATH=str(root),
        OPENAI_API_KEY='loopback-only', OPENAI_BASE_URL=base, PYTHONUNBUFFERED='1',
        HERMES_DISABLE_LAZY_INSTALLS='1', A2A_PORT=str(port))
    url = f'http://127.0.0.1:{port}/'
    marker = 'ORIGINAL_HUMAN_TARGET'
    visible = operation in {'unchanged', 'next-turn'}
    observations = []

    def diagnostic():
        return repr([(r['request_id'], r['status']) for r in read_state(home)[0]])

    def observe(stage):
        public = {method: rpc(url, method, {'taskId': target, 'includeArtifacts': True})
            for method in ['GetTask', 'SubscribeToTask']}
        public['ListTasks'] = rpc(url, 'ListTasks', {'includeArtifacts': True})
        public['replay'] = json.dumps(send(url, marker, 'target-input'))
        sibling_get = rpc(url, 'GetTask', {'taskId': healthy})
        observations.append({'stage': stage, 'visible': {k: marker in v for k, v in public.items()},
                             'healthy_visible': 'HEALTHY_SIBLING' in sibling_get})
        assert all((marker in text) is visible for text in public.values()), public
        assert 'HEALTHY_SIBLING' in sibling_get, sibling_get
        assert len(model.requests) == expected_calls
        assert raw_task(home, target) == before_raw
        warm_rec = warm.get(target)
        assert (warm_rec is not None) is visible
        if visible:
            assert marker in warm_rec['reply']
        return public

    try:
        with daemon(root, home, env, barrier=False) as (_, desc):
            completed = send(url, marker, 'target-input')
            result = completed['result']
            target = result.get('task', result)['id']
            assert marker in json.dumps(completed)
            sibling = json.loads(rpc(url, 'SendMessage', {'message': {'messageId': 'healthy-input',
                'contextId': 'healthy-context', 'parts': [{'text': 'HEALTHY_SIBLING'}]}}))
            healthy = sibling['result'].get('task', sibling['result'])['id']
            assert 'HEALTHY_SIBLING' in json.dumps(sibling)
            wait_for(lambda: len([v for v in read_state(home)[1].values()
                if v['parts']['text']['state'] == 'delivered']) == 2, diagnostic)
            value = next(v for v in read_state(home)[1].values() if v['reply_to'] == target)
            assert value['binding']['policy'] == 'human_reply_v1'
            before_raw = raw_task(home, target)
            assert 'durable_result_v1' not in json.loads(before_raw), 'control must use ordinary HUMAN completion'
            warm = protocol.TaskStore(home)
            assert marker in warm.get(target)['reply']
            expected_calls = len(model.requests)
            assert expected_calls == 2
            if operation == 'next-turn':
                second = send(url, 'NORMAL_FOLLOWING_TURN', 'following-input')
                assert 'NORMAL_FOLLOWING_TURN' in json.dumps(second), second
                expected_calls += 1
                with closing(sqlite3.connect(f'file:{home / "state.db"}?mode=ro', uri=True)) as db:
                    generation = db.execute('SELECT runtime_generation FROM sessions WHERE id=?', (value['binding']['session_id'],)).fetchone()[0]
                assert generation > value['binding']['generation']
            if operation in {'delete', 'rewind', 'deleted-missing-seal'}:
                mutation = asyncio.run(native_mutate(home, desc, value, 'delete' if operation == 'deleted-missing-seal' else operation))
                observations.append({'authenticated_mutation': mutation})
            if operation in {'missing-seal', 'malformed-seal', 'changed-seal-target', 'deleted-missing-seal'}:
                with sqlite3.connect(home / 'state.db') as db:
                    key = 'gateway.pending_final.v1:' + value['binding']['admission_id']
                    if operation in {'missing-seal', 'deleted-missing-seal'}:
                        db.execute('DELETE FROM state_meta WHERE key=?', (key,))
                    else:
                        damaged = '{broken' if operation == 'malformed-seal' else json.dumps({**value, 'reply_to': 'unrelated-task'})
                        db.execute('UPDATE state_meta SET value=? WHERE key=?', (damaged, key))
                observations.append({'isolated_fixture_corruption': operation})
            if operation not in {'session-store-reset', 'reset-resume'}:
                observe('warm_owner')
        if operation in {'session-store-reset', 'reset-resume'}:
            # Messaging reset has a canonical SessionStore entry, while session.mutate reset
            # requires a local-session policy. Exercise the supported store branch at rest.
            from hermes_state import SessionDB
            store = SessionStore(home / 'sessions', GatewayConfig())
            owned_db = SessionDB(home / 'state.db')
            store._db = owned_db
            try:
                route = value['binding']['route']
                entry = store.lookup_by_session_key(route)
                assert entry.session_id == value['binding']['session_id']
                reset = store.reset_session(route)
                assert reset.session_id != entry.session_id
                old = store._db.get_session(entry.session_id)
                assert old['end_reason'] == 'session_reset'
                observations.append({'canonical_reset': {'old': entry.session_id, 'new': reset.session_id,
                    'old_end_reason': old['end_reason']}})
                if operation == 'reset-resume':
                    resumed = store.switch_session(route, entry.session_id)
                    assert resumed.session_id == entry.session_id
                    assert owned_db.get_session(entry.session_id)['end_reason'] is None
                    observations.append({'canonical_resume': entry.session_id})
                assert warm.get(target) is None
            finally:
                store.close_all_db_handles()
                owned_db.close()
        for index in range(2):
            with daemon(root, home, env, barrier=False):
                observe('fresh_owner_' + str(index + 1))
                if operation not in {'missing-seal', 'malformed-seal', 'changed-seal-target', 'deleted-missing-seal'}:
                    part = next(v for v in read_state(home)[1].values() if v['reply_to'] == target)['parts']['text']
                    assert (part['state'], part['attempts']) == ('delivered', 1)
        prompts = [next((m.get('content', '') for m in reversed(body['messages']) if m['role'] == 'user'), '') for body in model.requests]
        assert sum(marker in str(prompt) for prompt in prompts) == 1, prompts
        print('A2A_NATIVE_LIFECYCLE ' + json.dumps({'operation': operation, 'task': target,
            'model_calls': len(model.requests), 'original_model_calls': 1, 'observations': observations}))
    finally:
        model.release.set()
        model.shutdown()
        model.server_close()
        thread.join(timeout=5)
