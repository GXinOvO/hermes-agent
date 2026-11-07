"""Additional controls at public RPC and the existing native recovery boundary."""
from copy import deepcopy
import io
import json
import os
from types import SimpleNamespace

import pytest

from gateway.config import PlatformConfig
from hermes_state import SessionDB
from plugins.platforms.a2a import protocol, durable_tasks as dt, task_bindings as tb
from plugins.platforms.a2a.adapter import A2AAdapter
from tests.gateway.test_a2a_durable_public import seed, recover


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path / 'user'))


@pytest.mark.parametrize('mutation', ['metadata', 'configuration', 'parts', 'boolean_number'])
def test_full_input_conflict_has_no_new_task_or_turn(tmp_path, mutation):
    seed(tmp_path)
    assert recover(tmp_path, 'recover')['delivered'] == 1
    adapter = A2AAdapter(PlatformConfig(enabled=True, extra={'port': 0}))
    params = {'message': {'messageId': 'one-input', 'contextId': 'real-context',
                          'parts': [{'text': 'do the work once'}]}}
    before = len(adapter.tasks.list(with_total=True)[0])
    changed = deepcopy(params)
    if mutation == 'metadata':
        changed['message']['metadata'] = {'semantic': 'new'}
    elif mutation == 'configuration':
        changed['configuration'] = {'acceptedOutputModes': ['application/json']}
    elif mutation == 'parts':
        changed['message']['parts'].append({'data': {'same_text_is_not_same_input': True}})
    else:
        changed['message']['metadata'] = {'semantic': True}
    response = adapter._rpc_message_send('conflict', changed, 'ip:127.0.0.1')
    assert response['error']['code'] == protocol.ERR_INVALID_PARAMS
    assert 'sealed native answer' not in json.dumps(response)
    assert len(adapter.tasks.list(with_total=True)[0]) == before
    assert not adapter._turns._turns
    assert (tmp_path / 'executions').read_text() == '1'


def test_live_owner_excluded_but_true_orphan_fails(tmp_path, monkeypatch):
    from gateway.delivery_ledger import _start_time
    a = A2AAdapter(PlatformConfig(enabled=True, extra={'port': 0}))
    a.tasks.create('live', 'ctx-live', 'peer')
    a.tasks.create('dead', 'ctx-dead', 'peer')
    a.tasks.activate('live')
    b = A2AAdapter(PlatformConfig(enabled=True, extra={'port': 0}))
    clock = protocol.time.time()
    monkeypatch.setattr(protocol, 'time', SimpleNamespace(time=lambda: clock + 1000))
    assert b.tasks.fail_orphans(1) == ['dead']
    assert b.tasks.get('live')['state'] == protocol.STATE_SUBMITTED
    assert b.tasks.get('live')['owner_started_at'] == _start_time(os.getpid())
    assert b.tasks.get('dead')['state'] == protocol.STATE_FAILED


@pytest.mark.parametrize('mutation', ['peer', 'input_id', 'agent_scope', 'input_hash'])
def test_corrupt_task_preserved_and_legal_recovery_still_works(tmp_path, mutation):
    identity = seed(tmp_path)
    db = SessionDB(tmp_path / 'state.db')
    key = dt._key(identity['task'])
    with db._read_ctx() as conn:
        original = conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()[0]
    damaged = json.loads(original)
    if mutation == 'agent_scope':
        damaged.update(agent_slug='other', tenant='other')
    elif mutation == 'input_hash':
        damaged['input_hash'] = '0' * 64
    else:
        damaged[mutation] = 'changed'
    raw = json.dumps(damaged)
    db._execute_write(lambda c: c.execute('UPDATE state_meta SET value=? WHERE key=?', (raw, key)))
    first = recover(tmp_path, 'recover')
    assert first['delivered'] == 0
    assert (first['part']['state'], first['part']['attempts']) == ('pending', 0)
    with db._read_ctx() as conn:
        assert conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()[0] == raw
    db._execute_write(lambda c: c.execute('UPDATE state_meta SET value=? WHERE key=?', (original, key)))
    db.close()
    second = recover(tmp_path, 'recover')
    assert second['delivered'] == 1
    assert second['executions'] == '1'
    assert 'sealed native answer' in second['public']['GetTask']


def test_terminal_conflict_refuses_before_claim(tmp_path):
    identity = seed(tmp_path)
    a = A2AAdapter(PlatformConfig(enabled=True, extra={'port': 0}))
    a.tasks.complete(identity['task'], protocol.STATE_CANCELED)
    result = recover(tmp_path, 'recover')
    assert result['delivered'] == 0
    assert (result['part']['state'], result['part']['attempts']) == ('pending', 0)


def test_genuine_new_inputs_still_hit_turn_limit():
    a = A2AAdapter(PlatformConfig(enabled=True, extra={'port': 0}))
    for index in range(protocol.max_pingpong_turns()):
        task, _ = a._prepare_task({'message': {'messageId': str(index), 'contextId': 'budget',
                                             'parts': [{'text': 'new turn'}]}}, 'peer')
        assert task['status']['state'] != protocol.STATE_REJECTED
    task, _ = a._prepare_task({'message': {'messageId': 'overflow', 'contextId': 'budget',
                                         'parts': [{'text': 'new turn'}]}}, 'peer')
    assert task['status']['state'] == protocol.STATE_REJECTED


@pytest.mark.parametrize('method', ['get', 'list', 'delete'])
def test_push_crud_exact_scope_and_legitimate_owner(method):
    a = A2AAdapter(PlatformConfig(enabled=True, extra={'port': 0, 'agents': {
        'other': {'local': True, 'tenant': 'other'}}}))
    a.tasks.create('other-task', 'ctx', 'peer', 'other', 'other')
    agent = a._agents['other']
    created = a._rpc_push_config_create('create', {'taskId': 'other-task',
        'pushNotificationConfig': {'url': 'http://127.0.0.1:9/inert'}}, agent=agent)
    assert 'result' in created
    operation = getattr(a, '_rpc_push_config_' + method)
    assert operation('root', {'taskId': 'other-task'})['error']['code'] == protocol.ERR_TASK_NOT_FOUND
    assert 'result' in operation('owner', {'taskId': 'other-task'}, agent=agent)


def test_subscription_timeout_releases_watcher(monkeypatch):
    import plugins.platforms.a2a.adapter as module
    monkeypatch.setattr(module, '_reply_timeout', lambda: 0)
    a = A2AAdapter(PlatformConfig(enabled=True, extra={'port': 0}))
    a.tasks.create('waiting', 'ctx', 'peer')
    class Handler:
        wfile = io.BytesIO()
        def send_response(self, *_args):
            pass
        def send_header(self, *_args):
            pass
        def end_headers(self):
            pass
    a._rpc_tasks_subscribe(Handler(), 'timeout', {'taskId': 'waiting'})
    assert not a.tasks._watchers


def test_existing_db_reads_never_open_writer(tmp_path, monkeypatch):
    a = A2AAdapter(PlatformConfig(enabled=True, extra={'port': 0}))
    a.tasks.create('readable', 'ctx', 'peer')
    opened = []
    original = dt.SessionDB
    def read_only(*args, **kwargs):
        opened.append(kwargs.get('read_only', False))
        return original(*args, **kwargs)
    monkeypatch.setattr(dt, 'SessionDB', read_only)
    b = A2AAdapter(PlatformConfig(enabled=True, extra={'port': 0}))
    assert 'result' in b._rpc_tasks_get('get', {'taskId': 'readable'})
    assert b._rpc_tasks_list('list', {})['result']['totalSize'] == 1
    assert opened and all(opened)


def test_canonical_preserves_json_types_and_order():
    left = {'message': {'parts': [{'data': {'a': True, 'b': 1}}]}, 'metadata': {'x': 'y'}}
    reordered = {'metadata': {'x': 'y'}, 'message': {'parts': [{'data': {'b': 1, 'a': True}}]}}
    number = deepcopy(left)
    number['message']['parts'][0]['data']['a'] = 1
    assert tb.digest(tb.canonical_input(left, 'ctx')) == tb.digest(tb.canonical_input(reordered, 'ctx'))
    assert tb.digest(tb.canonical_input(left, 'ctx')) != tb.digest(tb.canonical_input(number, 'ctx'))


def test_live_real_http_subscriber_observes_fresh_process_publication(tmp_path, monkeypatch):
    import asyncio
    import http.client
    import threading
    import plugins.platforms.a2a.adapter as module
    monkeypatch.setattr(module, '_SSE_KEEPALIVE', 0.05)
    identity = seed(tmp_path)

    async def scenario():
        a = A2AAdapter(PlatformConfig(enabled=True, extra={'port': 0}))
        assert await a.connect()
        port = a._httpd.server_address[1]
        started = threading.Event()
        received = {}
        def subscriber():
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
            try:
                payload = {'jsonrpc':'2.0', 'id':'real-subscriber', 'method':'tasks/subscribe',
                           'params':{'taskId':identity['task']}}
                connection.request('POST', '/', json.dumps(payload), {'Content-Type':'application/json'})
                response = connection.getresponse()
                assert response.status == 200
                assert response.getheader('Content-Type').startswith('text/event-stream')
                started.set()
                received['wire'] = response.read().decode()
            except BaseException as exc:
                received['error'] = repr(exc)
                started.set()
            finally:
                connection.close()
        thread = threading.Thread(target=subscriber)
        thread.start()
        try:
            assert await asyncio.to_thread(started.wait, 3)
            result = await asyncio.to_thread(recover, tmp_path, 'recover')
            assert result['delivered'] == 1
            await asyncio.to_thread(thread.join, 5)
            assert not thread.is_alive()
            assert 'error' not in received
            assert 'sealed native answer' in received['wire']
            assert protocol.STATE_COMPLETED in received['wire']
            assert not a.tasks._watchers
        finally:
            await a.disconnect()
    asyncio.run(scenario())
