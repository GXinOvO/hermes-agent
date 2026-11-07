from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import io
import json
import threading
from types import SimpleNamespace

import pytest

from gateway.config import PlatformConfig
from hermes_state import SessionDB
from plugins.platforms.a2a import durable_tasks as dt, protocol
from plugins.platforms.a2a.adapter import A2AAdapter
from tests.gateway import test_a2a_durable_public as public


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path / 'user'))


def snapshot(home):
    db = SessionDB(home / 'state.db', read_only=True)
    try:
        with db._read_ctx() as conn:
            return list(conn.execute('SELECT key,value FROM state_meta ORDER BY key'))
    finally:
        db.close()


def test_capacity_refuses_before_task_handler_or_conversation_turn(tmp_path, monkeypatch):
    monkeypatch.setattr(dt, 'MAX_IDENTITIES', 1, raising=False)
    adapter = A2AAdapter(PlatformConfig(enabled=True, extra={'port': 0}))
    params = {'message': {'messageId': 'accepted', 'contextId': 'capacity',
                          'parts': [{'text': 'do not execute twice'}]}}
    first, _pending = adapter._prepare_task(params, 'peer')
    assert first is not None and first['status']['state'] in protocol.TERMINAL_STATES
    before = snapshot(tmp_path)
    turns = deepcopy(adapter._turns._turns)
    second = {'message': {'messageId': 'refused', 'contextId': 'capacity',
                          'parts': [{'text': 'refuse before execution'}]}}
    response = adapter._rpc_message_send('new', second, 'peer')
    assert response.get('error', {}).get('code') == protocol.ERR_RATE_LIMITED, response
    assert 'capacity' in response['error']['message'].lower()
    assert snapshot(tmp_path) == before
    assert adapter._turns._turns == turns
    replay = adapter._rpc_message_send('replay', params, 'peer')
    assert replay['result']['id'] == first['id']
    assert snapshot(tmp_path) == before


def test_confirmed_result_retained_seven_days_then_fenced_without_payload(tmp_path, monkeypatch):
    identity = public.seed(tmp_path)
    assert public.recover(tmp_path, 'recover')['delivered'] == 1
    adapter = A2AAdapter(PlatformConfig(enabled=True, extra={'port': 0}))
    clock = protocol.time.time()
    monkeypatch.setattr(dt, 'time', SimpleNamespace(time=lambda: clock), raising=False)
    adapter._fail_orphans_once()
    monkeypatch.setattr(dt, 'time', SimpleNamespace(time=lambda: clock + 6 * 24 * 3600), raising=False)
    adapter._fail_orphans_once()
    assert 'sealed native answer' in json.dumps(adapter._rpc_tasks_get('kept', {'taskId': identity['task']}))
    monkeypatch.setattr(dt, 'time', SimpleNamespace(time=lambda: clock + 7 * 24 * 3600 + 1), raising=False)
    adapter._fail_orphans_once()
    expired = adapter._rpc_tasks_get('expired', {'taskId': identity['task']})
    assert expired.get('error', {}).get('code') == protocol.ERR_TASK_NOT_FOUND, expired
    retained = [(key, raw) for key, raw in snapshot(tmp_path) if key.startswith(('a2a.task.', 'a2a.ingress.'))]
    assert retained and all('sealed native answer' not in raw and 'do the work once' not in raw for _key, raw in retained)
    fresh = A2AAdapter(PlatformConfig(enabled=True, extra={'port': 0}))
    replay = fresh._rpc_message_send('replay-expired', {'message': {
        'role': 'ROLE_USER', 'messageId': 'one-input', 'contextId': 'real-context',
        'parts': [{'text': 'do the work once'}]}}, 'ip:127.0.0.1')
    assert replay.get('error', {}).get('code') == protocol.ERR_INVALID_PARAMS, replay
    assert 'expired' in replay['error']['message'].lower()
    assert (tmp_path / 'executions').read_text() == '1'


@pytest.mark.parametrize('outcome', ['pending', 'unknown'])
def test_retention_preserves_unconfirmed_result_raw(tmp_path, monkeypatch, outcome):
    public.seed(tmp_path)
    if outcome == 'unknown':
        assert public.recover(tmp_path, 'lost')['part']['state'] == 'unknown'
    adapter = A2AAdapter(PlatformConfig(enabled=True, extra={'port': 0}))
    before = snapshot(tmp_path)
    clock = protocol.time.time()
    monkeypatch.setattr(dt, 'time', SimpleNamespace(time=lambda: clock + 100 * 24 * 3600), raising=False)
    adapter._fail_orphans_once()
    assert snapshot(tmp_path) == before


def test_capacity_counts_bad_raw_and_keeps_stream_replay_available(tmp_path, monkeypatch):
    monkeypatch.setattr(dt, 'MAX_IDENTITIES', 2)
    adapter = A2AAdapter(PlatformConfig(enabled=True, extra={'port': 0}))
    adapter.tasks.create('existing', 'ctx', 'peer')
    db = SessionDB(tmp_path / 'state.db')
    try:
        db._execute_write(lambda conn: conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)',
                                                   (dt._key('bad-raw'), '{broken')))
    finally:
        db.close()
    before = snapshot(tmp_path)
    class Handler:
        wfile = io.BytesIO()
        def send_response(self, *_args):
            pass
        def send_header(self, *_args):
            pass
        def end_headers(self):
            pass
    handler = Handler()
    adapter._rpc_message_stream(handler, 'capacity', {'message': {
        'messageId': 'new', 'contextId': 'ctx', 'parts': [{'text': 'not admitted'}]}}, 'peer')
    wire = handler.wfile.getvalue().decode()
    assert 'capacity' in wire and str(protocol.ERR_RATE_LIMITED) in wire
    assert snapshot(tmp_path) == before
    assert adapter.tasks.get('existing')['task_id'] == 'existing'
    assert not adapter._turns._turns


def test_last_identity_capacity_is_shared_across_two_writers(tmp_path, monkeypatch):
    monkeypatch.setattr(dt, 'MAX_IDENTITIES', 1)
    stores = [protocol.TaskStore(tmp_path), protocol.TaskStore(tmp_path)]
    barrier = threading.Barrier(2)
    def create(index):
        barrier.wait(timeout=10)
        try:
            return stores[index].create('task-' + str(index), 'ctx', 'peer')['task_id']
        except dt.CapacityExceeded:
            return 'refused'
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(create, (0, 1)))
    assert results.count('refused') == 1
    assert len(dt.load(tmp_path)) == 1
    assert sum(key.startswith(dt.PREFIX) for key, _raw in snapshot(tmp_path)) == 1


def test_expired_fence_cannot_be_mutated_by_stale_store(tmp_path, monkeypatch):
    identity = public.seed(tmp_path)
    assert public.recover(tmp_path, 'recover')['delivered'] == 1
    stale = A2AAdapter(PlatformConfig(enabled=True, extra={'port': 0}))
    cleaner = A2AAdapter(PlatformConfig(enabled=True, extra={'port': 0}))
    clock = protocol.time.time()
    monkeypatch.setattr(dt, 'time', SimpleNamespace(time=lambda: clock))
    cleaner._fail_orphans_once()
    monkeypatch.setattr(dt, 'time', SimpleNamespace(time=lambda: clock + 7 * 24 * 3600 + 1))
    cleaner._fail_orphans_once()
    before = snapshot(tmp_path)
    with pytest.raises(RuntimeError, match='expired'):
        stale.tasks.set_push_config(identity['task'], 'http://127.0.0.1:9/inert')
    assert snapshot(tmp_path) == before


def test_bad_ack_and_corrupt_sibling_survive_retention(tmp_path, monkeypatch):
    from gateway import pending_final as pf
    public.seed(tmp_path)
    assert public.recover(tmp_path, 'recover')['delivered'] == 1
    db = SessionDB(tmp_path / 'state.db')
    try:
        value = pf.pending(db)[0]
        value['parts']['text']['delivered_at'] = True
        def damage(conn):
            conn.execute('UPDATE state_meta SET value=? WHERE key=?',
                         (json.dumps(value), pf.PREFIX + value['binding']['admission_id']))
            conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)', (dt._key('dirty'), '{broken'))
        db._execute_write(damage)
    finally:
        db.close()
    adapter = A2AAdapter(PlatformConfig(enabled=True, extra={'port': 0}))
    before = snapshot(tmp_path)
    clock = protocol.time.time()
    monkeypatch.setattr(dt, 'time', SimpleNamespace(time=lambda: clock + 100 * 24 * 3600))
    adapter._fail_orphans_once()
    assert snapshot(tmp_path) == before


def test_confirmed_public_result_expires_after_core_payload_compaction(tmp_path, monkeypatch):
    from gateway import pending_final as pf
    identity = public.seed(tmp_path)
    assert public.recover(tmp_path, 'recover')['delivered'] == 1
    clock = protocol.time.time() + 8 * 24 * 3600
    monkeypatch.setattr(dt, 'time', SimpleNamespace(time=lambda: clock))
    monkeypatch.setattr(pf, 'time', SimpleNamespace(time=lambda: clock))
    db = SessionDB(tmp_path / 'state.db')
    try:
        pf.reconcile_orphans(db)
        assert pf.pending(db)[0]['expired'] is True
        assert 'content' not in pf.pending(db)[0]['parts']['text']
    finally:
        db.close()
    adapter = A2AAdapter(PlatformConfig(enabled=True, extra={'port': 0}))
    adapter._fail_orphans_once()
    response = adapter._rpc_tasks_get('expired', {'taskId': identity['task']})
    assert response.get('error', {}).get('code') == protocol.ERR_TASK_NOT_FOUND, response
