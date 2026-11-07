import json
import queue

import pytest

from tools import async_delegation as ad


@pytest.fixture(autouse=True)
def isolated_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    ad._reset_for_tests()
    yield
    ad._reset_for_tests()


@pytest.mark.parametrize('column,damaged', [('task_json', '{'), ('task_json', '[]'),
    ('task_json', '{"task_transcripts":{"0":[]}}'), ('task_json', '{"owner_cwd":{}}'),
    ('task_json', '{"goals":{}}'), ('task_json', '{"task_indexes":[true]}'),
    ('result_json', '{'), ('result_json', '[]'), ('result_json', '{"partial":true,"results":[null]}'),
    ('result_json', '{"partial":true,"results":{}}')])
def test_abandoned_bad_row_does_not_block_durable_completed_siblings(monkeypatch, column, damaged):
    monkeypatch.setattr(ad, '_owner_liveness', lambda: lambda pid, started: False)
    monkeypatch.setattr('tools.async_delegation_recovery_hints.git_state_hint', lambda *args: None)
    def never_execute(*args, **kwargs):
        raise AssertionError('recovery must not start an executor')
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
    with ad._DB_LOCK, ad._transaction() as conn:
        if column == 'task_json':
            conn.execute('UPDATE async_delegations SET task_json=? WHERE delegation_id=?', (damaged, 'bad'))
        else:
            conn.execute('UPDATE async_delegations SET result_json=? WHERE delegation_id=?', (damaged, 'bad'))
        before = conn.execute('SELECT task_json,result_json,state FROM async_delegations WHERE delegation_id=?', ('bad',)).fetchone()
    target = queue.Queue()
    assert ad.restore_undelivered_completions(target) == 2
    restored = {event['delegation_id']: event for event in list(target.queue)}
    assert set(restored) == {'good', 'partial'}
    assert restored['good']['summary'] == 'retained'
    assert restored['partial']['status'] == 'unknown'
    assert restored['partial']['results'][0]['summary'] == 'first child'
    assert restored['partial']['results'][1]['status'] == 'unknown'
    assert ad.restore_undelivered_completions(queue.Queue()) == 0
    with ad._DB_LOCK, ad._transaction() as conn:
        assert conn.execute('SELECT task_json,result_json,state FROM async_delegations WHERE delegation_id=?', ('bad',)).fetchone() == before
