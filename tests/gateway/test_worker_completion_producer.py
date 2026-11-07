from contextlib import contextmanager
import json
import os
import queue
import time

import psutil
import pytest

from agent.runtime_session_store import RuntimeSessionStore
from gateway.session_worker_delegation import persist_worker_delegation
from hermes_cli.process_identity import worker_birth
from hermes_state import SessionDB
from hermes_state_runtime import begin_runtime_epoch, mutate_worker_execution, register_worker_execution
from tools import async_delegation as ad
from tools.async_delegation_worker import WorkerDelegationLedger


@contextmanager
def worker_store(home):
    with SessionDB(home / 'state.db') as db:
        db.create_session('parent', source='cli')
        epoch = begin_runtime_epoch(db, instance_id='owner')
        db._execute_write(ad._initialize_schema)
        register_worker_execution(db, epoch=epoch, execution_id='worker', session_id='parent',
            generation=0, kind='compute', adoption_secret='inert')
        scope = dict(epoch=epoch, execution_id='worker', session_id='parent', generation=0,
            pid=os.getpid(), birth=worker_birth(psutil.Process()))
        requests = []
        def rpc(method, **params):
            assert method == 'worker.persist'
            requests.append(json.loads(json.dumps(params)))
            pid, birth = params.pop('pid'), params.pop('birth')
            if params['operation'].startswith('delegation.'):
                return persist_worker_delegation(db, **params, worker_pid=pid, worker_birth=birth)
            return mutate_worker_execution(db, **params)
        store = RuntimeSessionStore(rpc, scope, home / 'outbox')
        try:
            yield db, store, requests
        finally:
            if store.failure:
                store._outbox_owner.close()
            else:
                store.close()


@pytest.mark.parametrize('batch', [False, True])
def test_real_completion_producer_commits_through_worker_outbox(tmp_path, monkeypatch, batch):
    events = queue.Queue()
    from tools.process_registry import process_registry
    monkeypatch.setattr(process_registry, 'completion_queue', events)
    with worker_store(tmp_path) as (db, store, _requests):
        remote = WorkerDelegationLedger(store)
        monkeypatch.setattr(ad, 'worker_ledger', lambda:remote)
        record = dict(delegation_id='deleg_saved', parent_session_id='parent', session_key='',
            origin_ui_session_id='', origin_session_id='', dispatched_at=time.time(), completed_at=time.time(),
            goal='inert completion', context=None, toolsets=None, role='leaf', model=None)
        result = {'summary':'sealed result'}
        if batch:
            record.update(is_batch=True, goals=['one','two'])
            result = {'results':[{'task_index':i,'status':'completed','summary':f'saved-{i}'} for i in range(2)]}
        remote.dispatch(record)
        ad._push_completion_event(record, result, 'completed')
        event = events.get_nowait()
        assert event['result_durability'] == 'persisted'
        assert store.failure is None and store.journal['pending'] == []
        with db._read_ctx() as conn:
            row = conn.execute('SELECT state,event_json,result_json FROM async_delegations WHERE delegation_id=?', ('deleg_saved',)).fetchone()
            assert row[0] == 'completed'
            assert json.loads(row[1])['result_durability'] == 'persisted'
            assert json.loads(row[2]) == result
        assert store.append_messages_batch('parent', [{'role':'assistant','content':'after completion'}]) == 1
        assert store.finish() == {'status':'terminal'}


def test_capacity_refusal_survives_lost_ack_and_outbox_reopen(tmp_path, monkeypatch):
    path = tmp_path / 'state.db'
    db = SessionDB(path)
    db.create_session('parent', source='cli')
    epoch = begin_runtime_epoch(db, instance_id='owner')
    db._execute_write(ad._initialize_schema)
    register_worker_execution(db, epoch=epoch, execution_id='worker', session_id='parent',
        generation=0, kind='compute', adoption_secret='inert')
    scope = dict(epoch=epoch, execution_id='worker', session_id='parent', generation=0,
        pid=os.getpid(), birth=worker_birth(psutil.Process()))
    db._execute_write(lambda conn:conn.execute("INSERT INTO async_delegations "
        "(delegation_id,origin_session,state,dispatched_at,updated_at,delivery_state) "
        "VALUES('saved','','completed',1,1,'pending')"))
    monkeypatch.setattr(ad, '_MAX_DURABLE_PENDING', 1)
    lost = [True]
    def rpc(method, **params):
        assert method == 'worker.persist'
        pid, birth = params.pop('pid'), params.pop('birth')
        result = (persist_worker_delegation(db, **params, worker_pid=pid, worker_birth=birth)
            if params['operation'].startswith('delegation.') else mutate_worker_execution(db, **params))
        if lost.pop() if lost else False:
            raise TimeoutError('refusal ACK lost after commit')
        return result
    store = RuntimeSessionStore(rpc, scope, tmp_path / 'outbox')
    try:
        monkeypatch.setattr(ad, 'worker_ledger', lambda:WorkerDelegationLedger(store))
        with pytest.raises(TimeoutError, match='refusal ACK lost'):
            ad.dispatch_async_delegation(goal='never run', context=None, toolsets=None, role='leaf', model=None,
                session_key='', parent_session_id='parent', runner=lambda:pytest.fail('runner started'))
        assert not ad.active_count() and len(store.journal['pending']) == 1
        with db._read_ctx() as conn:
            receipt = json.loads(conn.execute('SELECT result_json FROM worker_receipts').fetchone()[0])
        assert receipt == {'value':{'status':'rejected','error':'delegation_result_capacity'}}
        store._outbox_owner.close()
        db.close()
        db = SessionDB(path)
        db._execute_write(lambda conn:conn.execute('DELETE FROM async_delegations WHERE delegation_id=?', ('saved',)))
        store = RuntimeSessionStore(rpc, scope, tmp_path / 'outbox')
        assert store.failure is None and store.journal['pending'] == []
        assert store.append_messages_batch('parent', [{'role':'assistant','content':'after durable refusal'}]) == 1
        assert store.finish() == {'status':'terminal'}
        with db._read_ctx() as conn:
            assert conn.execute('SELECT COUNT(*) FROM async_delegations').fetchone()[0] == 0
    finally:
        if not store._outbox_owner.closed:
            store.close()
        db.close()


@pytest.mark.parametrize('capacity', ['pending', 'identity'])
def test_capacity_refusal_consumes_receipt_and_allows_later_worker_persistence(tmp_path, monkeypatch, capacity):
    with worker_store(tmp_path) as (db, store, requests):
        db._execute_write(lambda conn:conn.execute(
            "INSERT INTO async_delegations(delegation_id,origin_session,state,dispatched_at,updated_at,delivery_state,result_json) "
            "VALUES('saved','', 'completed',1,1,?,?)", ('pending' if capacity == 'pending' else 'delivered', '{"summary":"old"}')))
        monkeypatch.setattr(ad, '_MAX_DURABLE_PENDING' if capacity == 'pending' else '_MAX_DURABLE_IDENTITIES', 1)
        monkeypatch.setattr(ad, 'worker_ledger', lambda:WorkerDelegationLedger(store))
        ran = []
        response = ad.dispatch_async_delegation(goal='must refuse', context=None, toolsets=None, role='leaf',
            model=None, session_key='', parent_session_id='parent', runner=lambda:ran.append('unexpected') or {})
        assert response['status'] == 'rejected' and 'capacity' in response['error']
        assert ran == [] and not ad.active_count()
        assert store.failure is None and store.journal['pending'] == []
        db._execute_write(lambda conn:conn.execute('DELETE FROM async_delegations WHERE delegation_id=?', ('saved',)))
        request = requests[0]
        pid, birth = request.pop('pid'), request.pop('birth')
        replay = persist_worker_delegation(db, **request, worker_pid=pid, worker_birth=birth)
        assert replay['value'] == response
        with db._read_ctx() as conn:
            assert conn.execute('SELECT COUNT(*) FROM async_delegations').fetchone()[0] == 0
        assert store.append_messages_batch('parent', [{'role':'assistant','content':'capacity refused without poison'}]) == 1
        store.queue_token_counts('parent', input_tokens=1, output_tokens=1)
        assert store.finish() == {'status':'terminal'}
