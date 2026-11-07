"""Independent public-boundary probes using the frozen product entry points.

These tests state the required safe behavior. Failures are retained as review evidence.
"""
import asyncio
import io
import json
import threading
from types import SimpleNamespace

import pytest

from tests.gateway.test_a2a_durable_public import seed, recover
from tests.gateway.a2a_durable_peer import rpc
from gateway.config import PlatformConfig
from plugins.platforms.a2a.adapter import A2AAdapter, _orphan_timeout
from plugins.platforms.a2a import protocol


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path / 'user'))


def test_sealed_completion_survives_restart_watchdog(tmp_path, monkeypatch):
    identity = seed(tmp_path)
    adapter = A2AAdapter(PlatformConfig(enabled=True, extra={'port': 0}))
    clock = protocol.time.time()
    monkeypatch.setattr(protocol, 'time', SimpleNamespace(time=lambda: clock + _orphan_timeout() + 1))
    failed = adapter._fail_orphans_once()
    result = recover(tmp_path, 'recover')
    print('REVIEW_WATCHDOG', json.dumps({'failed': failed, 'task': identity, 'result': result}))
    assert result['delivered'] == 1, 'a sealed completion must survive its fresh adapter orphan sweep'
    assert 'sealed native answer' in result['public']['GetTask']


def test_existing_subscription_sees_other_store_completion(tmp_path, monkeypatch):
    identity = seed(tmp_path)
    import plugins.platforms.a2a.adapter as adapter_module
    monkeypatch.setattr(adapter_module, '_reply_timeout', lambda: 8)
    monkeypatch.setattr(adapter_module, '_SSE_KEEPALIVE', 0.1)
    async def run():
        adapter = A2AAdapter(PlatformConfig(enabled=True, extra={'port': 0}))
        ready = threading.Event()
        original = adapter.tasks.watch
        def watch(*args):
            future = original(*args)
            ready.set()
            return future
        adapter.tasks.watch = watch
        assert await adapter.connect()
        url = f'http://127.0.0.1:{adapter._httpd.server_port}/'
        try:
            waiting = asyncio.create_task(asyncio.to_thread(rpc, url, 'SubscribeToTask', {'taskId': identity['task']}))
            assert await asyncio.to_thread(ready.wait, 5)
            published = await asyncio.to_thread(recover, tmp_path, 'recover')
            assert published['delivered'] == 1
            streamed = await waiting
            fresh = await asyncio.to_thread(rpc, url, 'GetTask', {'taskId': identity['task']})
            print('REVIEW_CROSS_STORE', json.dumps({'stream': streamed, 'fresh_get': fresh}))
            assert 'sealed native answer' in streamed, 'live HTTP subscription must observe other-owner durable publication'
        finally:
            await adapter.disconnect()
    asyncio.run(run())


def test_changed_body_same_message_id_is_explicit_conflict(tmp_path):
    seed(tmp_path)
    assert recover(tmp_path, 'recover')['delivered'] == 1
    adapter = A2AAdapter(PlatformConfig(enabled=True, extra={'port': 0}))
    response = adapter._rpc_message_send('changed-body', {'message': {
        'role': 'ROLE_USER', 'messageId': 'one-input', 'contextId': 'real-context',
        'parts': [{'text': 'THIS IS DIFFERENT WORK'}]}}, 'ip:127.0.0.1', v1_response=True)
    print('REVIEW_CHANGED_BODY', json.dumps(response))
    assert 'error' in response, 'same input ID with changed work must return an explicit conflict'


def test_default_scope_cannot_mutate_other_agent_push_config():
    adapter = A2AAdapter(PlatformConfig(enabled=True, extra={'port': 0, 'agents': {
        'other': {'local': True, 'tenant': 'tenant-other'}}}))
    adapter.tasks.create('other-task', 'other-context', 'peer', 'other', 'tenant-other')
    hidden = adapter._rpc_tasks_get('get', {'taskId': 'other-task'})
    response = adapter._rpc_push_config_create('push', {'taskId': 'other-task',
        'pushNotificationConfig': {'url': 'http://127.0.0.1:9/forbidden'}})
    print('REVIEW_PUSH_SCOPE', json.dumps({'get': hidden, 'push': response}))
    assert 'error' in response, 'a root public RPC must not mutate a task hidden by its exact scope'


def test_stream_retry_preserves_completed_artifact(tmp_path):
    seed(tmp_path)
    assert recover(tmp_path, 'recover')['delivered'] == 1
    adapter = A2AAdapter(PlatformConfig(enabled=True, extra={'port': 0}))
    class Handler:
        def __init__(self):
            self.wfile = io.BytesIO()
        def send_response(self, *_args):
            pass
        def send_header(self, *_args):
            pass
        def end_headers(self):
            pass
    handler = Handler()
    adapter._rpc_message_stream(handler, 'stream-retry', {'message': {
        'role': 'ROLE_USER', 'messageId': 'one-input', 'contextId': 'real-context',
        'parts': [{'text': 'do the work once'}]}}, 'ip:127.0.0.1')
    raw = handler.wfile.getvalue().decode()
    print('REVIEW_STREAM_RETRY', raw)
    assert 'sealed native answer' in raw, 'a completed stream retry must return the retained artifact'


def test_corrupt_sibling_is_retained_without_hiding_valid_completion(tmp_path):
    from hermes_state import SessionDB
    from plugins.platforms.a2a.durable_tasks import PREFIX
    identity = seed(tmp_path)
    damaged = PREFIX + 'corrupt-review-row'
    db = SessionDB(tmp_path / 'state.db')
    db._execute_write(lambda conn: conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)', (damaged, '{broken')))
    result = recover(tmp_path, 'recover')
    with db._read_ctx() as conn:
        raw = conn.execute('SELECT value FROM state_meta WHERE key=?', (damaged,)).fetchone()[0]
    db.close()
    assert result['delivered'] == 1
    assert identity['task'] in result['public']['GetTask']
    assert raw == '{broken'


def test_retries_do_not_spend_new_conversation_turns(tmp_path):
    seed(tmp_path)
    assert recover(tmp_path, 'recover')['delivered'] == 1
    adapter = A2AAdapter(PlatformConfig(enabled=True, extra={'port': 0}))
    params = {'message': {'messageId': 'one-input', 'contextId': 'real-context',
                         'parts': [{'text': 'do the work once'}]}}
    count = protocol.max_pingpong_turns()
    for _ in range(count + 1):
        adapter._rpc_message_send('retry', params, 'ip:127.0.0.1')
    next_task, _ = adapter._prepare_task({'message': {'messageId': 'new-input', 'contextId': 'real-context',
        'parts': [{'text': 'new authorized turn'}]}}, 'ip:127.0.0.1')
    print('REVIEW_RETRY_BUDGET', json.dumps({'limit': count, 'task': next_task}))
    assert next_task['status']['state'] != protocol.STATE_REJECTED, 'retries must not exhaust anti-loop budget'
