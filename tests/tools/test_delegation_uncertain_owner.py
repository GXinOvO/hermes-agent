import queue
import subprocess
import sys

import pytest

from gateway import status
from tools import async_delegation as ad


@pytest.fixture(autouse=True)
def isolated_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    ad._reset_for_tests()
    yield
    ad._reset_for_tests()


@pytest.fixture
def foreign_owner():
    process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    try:
        started = status.get_process_start_time(process.pid)
        assert process.poll() is None and status._pid_exists(process.pid)
        assert started is not None and started > 0
        yield process, started
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)


def persist(task, *, batch=False):
    ad._persist_dispatch({'delegation_id':task, 'dispatched_at':ad.time.time(),
        'goal':task, 'is_batch':batch, 'goals':['first', 'second'] if batch else None,
        'session_key':'owner'})


def snapshot(task):
    with ad._DB_LOCK, ad._transaction() as conn:
        return conn.execute('SELECT * FROM async_delegations WHERE delegation_id=?', (task,)).fetchone()


def prohibit_executor(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('recovery must never start an executor')
    monkeypatch.setattr(ad, '_get_executor', forbidden)


@pytest.mark.parametrize('state', ['running', 'finalizing'])
@pytest.mark.parametrize('observed', ['unreadable', 'matching', 'drifted'])
def test_existing_owner_is_preserved_when_current_birth_is_unreadable(tmp_path, monkeypatch, foreign_owner, state, observed):
    process, started = foreign_owner
    prohibit_executor(monkeypatch)
    persist('protected', batch=True)
    ad.record_unit_child('protected', {'task_index':0, 'status':'completed', 'summary':'owned child'})
    persist('good')
    ad._persist_completion({'type':'async_delegation', 'delegation_id':'good', 'status':'completed',
        'completed_at':ad.time.time(), 'summary':'trusted'}, {'summary':'trusted'})
    persist('partial', batch=True)
    ad.record_unit_child('partial', {'task_index':0, 'status':'completed', 'summary':'finished child'})
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute('UPDATE async_delegations SET owner_pid=?,owner_started_at=?,state=? WHERE delegation_id=?',
                     (process.pid, started, state, 'protected'))
        conn.execute('UPDATE async_delegations SET owner_pid=NULL,owner_started_at=NULL WHERE delegation_id=?', ('partial',))
    before = snapshot('protected')
    original = status.get_process_start_time
    readings = {'unreadable':None, 'matching':started, 'drifted':started + status.START_TIME_DRIFT_TOLERANCE}
    monkeypatch.setattr(status, 'get_process_start_time', lambda pid:readings[observed] if pid == process.pid else original(pid))
    assert process.poll() is None and status._pid_exists(process.pid)
    target = queue.Queue()
    assert ad.restore_undelivered_completions(target) == 2
    events = {event['delegation_id']:event for event in list(target.queue)}
    assert set(events) == {'good', 'partial'}
    assert events['good']['summary'] == 'trusted'
    assert events['partial']['status'] == 'unknown'
    assert events['partial']['results'][0]['summary'] == 'finished child'
    assert events['partial']['results'][1]['status'] == 'unknown'
    assert snapshot('protected') == before
    assert ad.restore_undelivered_completions(queue.Queue()) == 0
    assert snapshot('protected') == before
    assert process.poll() is None


def test_orphan_sweep_leaves_foreign_owner_completion_when_birth_is_unreadable(monkeypatch, foreign_owner):
    process, started = foreign_owner
    prohibit_executor(monkeypatch)
    persist('protected')
    ad._persist_completion({'type':'async_delegation', 'delegation_id':'protected', 'status':'completed',
        'completed_at':ad.time.time(), 'summary':'foreign result'}, {'summary':'foreign result'})
    now = ad.time.time()
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute('UPDATE async_delegations SET owner_pid=?,owner_started_at=?,updated_at=? WHERE delegation_id=?',
                     (process.pid, started, now - ad._CLAIM_LEASE_S - ad._ORPHAN_STALE_S - 1, 'protected'))
    before = snapshot('protected')
    original = status.get_process_start_time
    monkeypatch.setattr(status, 'get_process_start_time', lambda pid:None if pid == process.pid else original(pid))
    target = queue.Queue()
    assert ad.sweep_orphaned_completions(target, now=now) == 0
    assert target.empty()
    assert snapshot('protected') == before
    assert ad.sweep_orphaned_completions(target, now=now + ad._CLAIM_LEASE_S + 1) == 0
    assert target.empty() and snapshot('protected') == before
    assert process.poll() is None


@pytest.mark.parametrize('observed', ['dead', 'recycled'])
def test_proven_dead_or_recycled_owner_still_recovers_once(monkeypatch, foreign_owner, observed):
    process, started = foreign_owner
    prohibit_executor(monkeypatch)
    persist('old')
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute('UPDATE async_delegations SET owner_pid=?,owner_started_at=? WHERE delegation_id=?',
                     (process.pid, started, 'old'))
    if observed == 'dead':
        process.terminate()
        process.wait(timeout=10)
        assert not status._pid_exists(process.pid)
    else:
        original = status.get_process_start_time
        monkeypatch.setattr(status, 'get_process_start_time',
            lambda pid:started + status.START_TIME_DRIFT_TOLERANCE + 1 if pid == process.pid else original(pid))
    target = queue.Queue()
    assert ad.restore_undelivered_completions(target) == 1
    event = target.get_nowait()
    assert event['delegation_id'] == 'old' and event['status'] == 'unknown'
    assert ad.restore_undelivered_completions(queue.Queue()) == 0
