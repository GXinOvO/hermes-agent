import json
import os
import queue

import pytest

from gateway.status import get_process_start_time
from tools import async_delegation as ad


@pytest.fixture(autouse=True)
def isolated_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    ad._reset_for_tests()
    yield
    ad._reset_for_tests()


@pytest.mark.parametrize('damage', ['nul_transcript', 'positive_infinity', 'negative_infinity', 'nan',
    'live_owner', 'unreadable_liveness', 'unreadable_diagnostics'])
def test_real_liveness_and_diagnostics_isolate_damaged_abandoned_row(monkeypatch, tmp_path, damage):
    def never_execute(*args, **kwargs):
        raise AssertionError('recovery must not start a runner')
    monkeypatch.setattr(ad, '_get_executor', never_execute)
    def persist(task, *, batch=False):
        ad._persist_dispatch({'delegation_id': task, 'dispatched_at': ad.time.time(),
            'goal': task, 'is_batch': batch, 'goals': ['first', 'second'] if batch else None,
            'session_key': 'owner'})
    persist('bad', batch=True)
    persist('good')
    persist('partial', batch=True)
    ad._persist_completion({'type':'async_delegation', 'delegation_id':'good', 'status':'completed',
        'completed_at':ad.time.time(), 'summary':'retained'}, {'summary':'retained'})
    ad.record_unit_child('partial', {'task_index':0, 'status':'completed', 'summary':'first child'})
    values = {'positive_infinity':float('inf'), 'negative_infinity':float('-inf'), 'nan':'NaN'}
    alive = ad._owner_liveness()
    assert alive(os.getpid(), get_process_start_time(os.getpid()))
    if damage == 'unreadable_liveness':
        def unreadable(pid, started):
            if pid == os.getpid():
                raise ValueError('injected unreadable process identity')
            return alive(pid, started)
        monkeypatch.setattr(ad, '_owner_liveness', lambda:unreadable)
    if damage == 'unreadable_diagnostics':
        from tools import async_delegation_recovery_hints as hints
        actual = hints.transcript_tails
        def unreadable(paths):
            if paths:
                raise ValueError('injected unreadable transcript path')
            return actual(paths)
        monkeypatch.setattr(hints, 'transcript_tails', unreadable)
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute('UPDATE async_delegations SET owner_pid=NULL,owner_started_at=NULL WHERE delegation_id=?', ('partial',))
        if damage in values or damage in ['live_owner', 'unreadable_liveness']:
            conn.execute('UPDATE async_delegations SET owner_pid=?,owner_started_at=? WHERE delegation_id=?',
                         (os.getpid(), values.get(damage, get_process_start_time(os.getpid())), 'bad'))

        else:
            task = json.loads(conn.execute('SELECT task_json FROM async_delegations WHERE delegation_id=?', ('bad',)).fetchone()[0])
            task['task_transcripts'] = {'0':'bad\0transcript' if damage == 'nul_transcript' else str(tmp_path / 'missing')}
            conn.execute('UPDATE async_delegations SET task_json=?,owner_pid=NULL,owner_started_at=NULL WHERE delegation_id=?',
                         (json.dumps(task), 'bad'))
        before = conn.execute('SELECT task_json,result_json,state,owner_pid,owner_started_at,event_json FROM async_delegations WHERE delegation_id=?', ('bad',)).fetchone()
    target = queue.Queue()
    assert ad.restore_undelivered_completions(target) == 2
    restored = {event['delegation_id']:event for event in list(target.queue)}
    assert set(restored) == {'good', 'partial'}
    assert restored['good']['summary'] == 'retained'
    assert restored['partial']['status'] == 'unknown'
    assert restored['partial']['results'][0]['summary'] == 'first child'
    assert restored['partial']['results'][1]['status'] == 'unknown'
    assert ad.restore_undelivered_completions(queue.Queue()) == 0
    with ad._DB_LOCK, ad._transaction() as conn:
        assert conn.execute('SELECT task_json,result_json,state,owner_pid,owner_started_at,event_json FROM async_delegations WHERE delegation_id=?', ('bad',)).fetchone() == before
