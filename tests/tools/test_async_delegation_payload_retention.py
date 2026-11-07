import json
import queue

from tools import async_delegation as ad


def add_rows(rows):
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.executemany("INSERT INTO async_delegations "
            "(delegation_id,origin_session,state,dispatched_at,updated_at,delivery_state,event_json,result_json) "
            "VALUES (?,'owner',?,1,?,?,?,?)", rows)


def test_pending_results_survive_history_pruning_and_remain_claimable(monkeypatch):
    monkeypatch.setattr(ad, '_MAX_RETAINED_COMPLETED', 1)
    monkeypatch.setattr(ad, '_MAX_DURABLE_PENDING', 1)
    add_rows([(f'pending-{i}', 'completed', i + 1, 'pending',
               json.dumps({'type':'async_delegation', 'delegation_id':f'pending-{i}'}),
               json.dumps({'summary':f'saved-{i}'})) for i in range(3)])
    ad._prune_durable_records()
    with ad._DB_LOCK, ad._transaction() as conn:
        rows = conn.execute('SELECT delegation_id,result_json FROM async_delegations ORDER BY delegation_id').fetchall()
    assert rows == [(f'pending-{i}', json.dumps({'summary':f'saved-{i}'})) for i in range(3)]
    assert ad.claim_completion_delivery('pending-0', 'consumer')
    assert ad.complete_completion_delivery('pending-0', 'consumer')
    assert not ad.claim_completion_delivery('pending-0', 'second')


def test_cleanup_keeps_minimal_delivery_fence_for_old_acknowledged_result(monkeypatch):
    monkeypatch.setattr(ad, '_MAX_RETAINED_COMPLETED', 0)
    add_rows([('old', 'completed', 1, 'delivered', json.dumps({'text':'private payload'}),
               json.dumps({'summary':'private output'}))])
    ad._prune_durable_records()
    with ad._DB_LOCK, ad._transaction() as conn:
        row = conn.execute('SELECT state,delivery_state,event_json,result_json FROM async_delegations WHERE delegation_id=?', ('old',)).fetchone()
    assert row is not None
    assert row[:2] == ('completed', 'delivered')
    assert row[2:] == (None, None)
    assert not ad.claim_completion_delivery('old', 'late')


def test_corrupt_completion_does_not_block_valid_sibling_replay():
    add_rows([('broken', 'completed', 1, 'pending', '{', '{}'),
              ('good', 'completed', 2, 'pending', json.dumps({'type':'async_delegation', 'delegation_id':'good'}), '{}')])
    target = queue.Queue()
    with ad._DB_LOCK, ad._transaction() as conn:
        rows = conn.execute('SELECT delegation_id,event_json,completed_at,dispatched_at FROM async_delegations ORDER BY delegation_id').fetchall()
        assert ad._replay_pending(conn, rows, target, 3) == 1
    assert target.get_nowait()['delegation_id'] == 'good'
    with ad._DB_LOCK, ad._transaction() as conn:
        assert conn.execute('SELECT event_json,delivery_state FROM async_delegations WHERE delegation_id=?', ('broken',)).fetchone() == ('{', 'pending')


def test_capacity_refuses_new_runner_without_evicting_existing_result(monkeypatch):
    import pytest
    from hermes_state_runtime import RuntimeStoreError
    monkeypatch.setattr(ad, '_MAX_DURABLE_PENDING', 1)
    add_rows([('saved', 'completed', 1, 'pending', '{}', '{"summary":"saved"}')])
    ran = []
    with pytest.raises(RuntimeStoreError, match='delegation_result_capacity'):
        ad.dispatch_async_delegation(goal='never execute', context=None, toolsets=None, role='leaf',
            model=None, session_key='capacity', runner=lambda:ran.append('unexpected') or {})
    assert ran == []
    assert not ad.active_count()
    with ad._DB_LOCK, ad._transaction() as conn:
        assert conn.execute('SELECT delegation_id,result_json FROM async_delegations').fetchall() == [('saved', '{"summary":"saved"}')]
