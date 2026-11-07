from contextlib import closing
import asyncio
import json
import queue
import subprocess
import sys
import time
from types import SimpleNamespace

import psutil
import pytest

from agent.runtime_session_store import RuntimeSessionStore
from gateway.session_contract import SessionRef
from gateway.session_worker import _claim, worker_request
from hermes_cli.process_identity import worker_birth
from hermes_state import SessionDB
from hermes_state_runtime import begin_runtime_epoch, register_worker_execution
from tools import async_delegation as ad
from tools.async_delegation_worker import WorkerDelegationLedger


@pytest.mark.parametrize('capacity', [None, 'pending', 'identity'])
def test_authenticated_bridge_uses_actual_completion_and_refusal_receipts(capacity, tmp_path, monkeypatch):
    from tools.process_registry import process_registry
    events = queue.Queue()
    monkeypatch.setattr(process_registry, 'completion_queue', events)
    worker = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        with SessionDB(tmp_path / 'state.db') as db:
            db.create_session('parent', source='cli')
            epoch = begin_runtime_epoch(db, instance_id='owner')
            db._execute_write(ad._initialize_schema)
            authority = SimpleNamespace(db=db, epoch=epoch, profile_id='profile',
                _require_admission_open=lambda:None, wake_after_worker=lambda sid:None)
            actor = SimpleNamespace(profile_id='profile', subject='alice', capabilities={'worker:adopt'})
            connection = SimpleNamespace(authority=authority, actor=actor)
            ref = SessionRef('profile', 'parent')
            scope = dict(profile_id='profile', session_id='parent', execution_id='worker', generation=0,
                pid=worker.pid, birth=worker_birth(psutil.Process(worker.pid)), secret='inert-worker-secret')
            claim = _claim(connection, ref, scope)
            register_worker_execution(db, epoch=epoch, execution_id='worker', session_id='parent', generation=0,
                kind='compute', adoption_secret=claim)
            requests = []
            def rpc(method, **params):
                assert method == 'worker.persist'
                params = dict(scope, **{k:v for k,v in params.items() if k not in scope})
                requests.append(json.loads(json.dumps(params)))
                return asyncio.run(worker_request(connection, ref, params, operation='persist'))
            if capacity:
                db._execute_write(lambda conn:conn.execute("INSERT INTO async_delegations "
                    "(delegation_id,origin_session,state,dispatched_at,updated_at,delivery_state) "
                    "VALUES('old','','completed',1,1,?)", ('pending' if capacity == 'pending' else 'delivered',)))
                monkeypatch.setattr(ad, '_MAX_DURABLE_PENDING' if capacity == 'pending' else '_MAX_DURABLE_IDENTITIES', 1)
            with closing(RuntimeSessionStore(rpc, dict(scope, epoch=epoch), tmp_path / 'bridge-outbox')) as store:
                remote = WorkerDelegationLedger(store)
                monkeypatch.setattr(ad, 'worker_ledger', lambda:remote)
                if capacity:
                    response = ad.dispatch_async_delegation(goal='refuse', context=None, toolsets=None, role='leaf',
                        model=None, session_key='', parent_session_id='parent', runner=lambda:pytest.fail('runner started'))
                    assert response == {'status':'rejected', 'error':'delegation_result_capacity'}
                    db._execute_write(lambda conn:conn.execute('DELETE FROM async_delegations WHERE delegation_id=?', ('old',)))
                    replay = rpc('worker.persist', **requests[0])
                    assert replay['value'] == response
                    assert events.empty()
                else:
                    record = dict(delegation_id='deleg_authenticated', parent_session_id='parent', session_key='',
                        origin_ui_session_id='', origin_session_id='', dispatched_at=time.time(), completed_at=time.time(),
                        goal='actual producer', context=None, toolsets=None, role='leaf', model=None)
                    remote.dispatch(record)
                    ad._push_completion_event(record, {'summary':'trusted'}, 'completed')
                    published = [events.get_nowait(), events.get_nowait()]
                    assert all(event['result_durability'] == 'persisted' for event in published)
                    assert sum(event.get('restored') is True for event in published) == 1
                assert store.failure is None and store.journal['pending'] == []
                assert store.append_messages_batch('parent', [{'role':'assistant','content':'still usable'}]) == 1
                store.queue_token_counts('parent', input_tokens=2, output_tokens=1)
                assert store.finish() == {'status':'terminal'}
                with db._read_ctx() as conn:
                    assert conn.execute('SELECT status FROM worker_executions WHERE execution_id=?', ('worker',)).fetchone()[0] == 'terminal'
    finally:
        worker.terminate()
        worker.wait(timeout=5)
