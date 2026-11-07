import json

import pytest

from hermes_state import SessionDB
from hermes_state_background import begin, complete, results


def binding(home, parent='parent'):
    return {'parent_session_id': parent, 'profile_id': str(home), 'event_type': 'background.complete'}


@pytest.mark.parametrize('damaged', ['{', '[]', '{}', '{"status":"running"}'])
def test_corrupt_record_does_not_block_admission_seal_or_reader(tmp_path, damaged):
    from hermes_state_background import PREFIX, pending_deliveries
    path = tmp_path / 'state.db'
    db = SessionDB(path)
    try:
        db.create_session('parent', source='cli')
        db._execute_write(lambda conn: conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)',
            (PREFIX + 'broken', damaged)))
        identity = binding(tmp_path)
        assert begin(db, 'trusted', identity)['claimed']
        complete(db, 'trusted', identity, {'task_id': 'trusted', 'text': 'only run'})
        messaging = binding(tmp_path, 'messaging-background')
        begin(db, 'message', messaging)
        complete(db, 'message', messaging, {'task_id': 'message', 'text': 'message'})
        assert {r['task_id'] for r in pending_deliveries(db)} == {'trusted', 'message'}
    finally:
        db.close()
    db = SessionDB(path)
    try:
        assert results(db, 'parent') == [{'event_type': 'background.complete',
            'payload': {'task_id': 'trusted', 'text': 'only run'}}]
        assert begin(db, 'trusted', identity)['status'] == 'completed'
        with db._read_ctx() as conn:
            assert conn.execute('SELECT value FROM state_meta WHERE key=?', (PREFIX + 'broken',)).fetchone()[0] == damaged
    finally:
        db.close()


def test_pending_side_results_all_survive_65_completions_and_fresh_reader(tmp_path):
    path = tmp_path / 'state.db'
    identity = binding(tmp_path)
    calls = []
    db = SessionDB(path)
    try:
        db.create_session('parent', source='cli')
        for i in range(65):
            task = f'bg_{i:02}'
            assert begin(db, task, identity)['claimed']
            calls.append(task)
            complete(db, task, identity, {'task_id': task, 'text': f'accepted-{i}'})
    finally:
        db.close()
    db = SessionDB(path)
    try:
        saved = results(db, 'parent')
        assert len(saved) == 65
        assert saved[0]['payload'] == {'task_id': 'bg_00', 'text': 'accepted-0'}
        assert results(db, 'parent') == saved
        assert all(begin(db, task, identity)['status'] == 'completed' for task in calls)
        assert len(calls) == 65
        assert db.get_messages('parent') == []
    finally:
        db.close()


def test_actual_canonical_side_producer_results_survive_history_pressure(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from gateway.session_contract import SessionRef
    from gateway.session_events import SessionEvents
    from gateway.session_tui_side import _spawn
    from gateway.session_background_results import read_snapshot
    from tests.waiting import wait_until
    monkeypatch.setattr('gateway.session_runtime_workers.persist_work_count', lambda *args: None)
    path = tmp_path / 'state.db'
    db = SessionDB(path)
    identity = binding(tmp_path)
    calls = []
    try:
        db.create_session('parent', source='cli')
        authority = SimpleNamespace(db=db, profile_id=str(tmp_path),
            sessions={'parent': SimpleNamespace(event_stream=SessionEvents())},
            runner=SimpleNamespace(config=SimpleNamespace(multiplex_profiles=False)))
        ref = SessionRef(str(tmp_path), 'parent')
        for i in range(65):
            task = f'bg_{i:02}'
            assert begin(db, task, identity)['claimed']
            def producer(task=task):
                calls.append(task)
                return 'accepted-' + task
            _spawn(authority, ref, task, 'background.complete', producer, identity=identity)
            wait_until(lambda: not authority._side_agents, timeout=5, what='side result sealed')
        saved = asyncio.run(read_snapshot(authority, ref))
        assert len(saved) == 65
        assert saved[0]['payload']['text'] == 'accepted-bg_00'
        assert asyncio.run(read_snapshot(authority, ref)) == saved
        assert len(calls) == 65
    finally:
        db.close()


@pytest.mark.parametrize('field,value', [('updated_at', 'bad'), ('owner_pid', 'bad'),
    ('binding', []), ('payload', []), ('parent_session_id', []), ('delivery_pid', 'bad')])
def test_mutated_real_seal_does_not_block_valid_sibling(tmp_path, field, value):
    from hermes_state_background import _key
    db = SessionDB(tmp_path / 'state.db')
    identity = binding(tmp_path)
    try:
        db.create_session('parent', source='cli')
        for task in ['damaged', 'trusted']:
            begin(db, task, identity)
            complete(db, task, identity, {'task_id': task, 'text': task})
        def damage(conn):
            raw = json.loads(conn.execute('SELECT value FROM state_meta WHERE key=?', (_key('damaged'),)).fetchone()[0])
            raw[field] = value
            text = json.dumps(raw)
            conn.execute('UPDATE state_meta SET value=? WHERE key=?', (text, _key('damaged')))
            return text
        damaged = db._execute_write(damage)
        assert results(db, 'parent') == [{'event_type': 'background.complete',
            'payload': {'task_id': 'trusted', 'text': 'trusted'}}]
        with db._read_ctx() as conn:
            assert conn.execute('SELECT value FROM state_meta WHERE key=?', (_key('damaged'),)).fetchone()[0] == damaged
    finally:
        db.close()


@pytest.mark.parametrize('delivery', ['pending', 'delivering', 'unknown', 'delivered'])
def test_expiry_preserves_delivery_fact_and_live_sender(tmp_path, monkeypatch, delivery):
    from hermes_state_background import _key, claim_delivery, finish_delivery, RETENTION_SECONDS
    import time
    db = SessionDB(tmp_path / 'state.db')
    identity = binding(tmp_path)
    try:
        db.create_session('parent', source='cli')
        begin(db, 'old', identity)
        complete(db, 'old', identity, {'task_id': 'old', 'text': 'accepted'})
        if delivery != 'pending':
            assert claim_delivery(db, 'old')
            if delivery != 'delivering':
                finish_delivery(db, 'old', delivery)
        def age(conn):
            value = json.loads(conn.execute('SELECT value FROM state_meta WHERE key=?', (_key('old'),)).fetchone()[0])
            value['created_at'] = time.time() - RETENTION_SECONDS - 1
            conn.execute('UPDATE state_meta SET value=? WHERE key=?', (json.dumps(value), _key('old')))
        db._execute_write(age)
        with monkeypatch.context() as live:
            live.setattr('cron.executions._owner_is_live', lambda *args: True)
            saved = results(db, 'parent')
        if delivery == 'delivering':
            assert saved[0]['payload']['text'] == 'accepted'
        with monkeypatch.context() as dead:
            dead.setattr('cron.executions._owner_is_live', lambda *args: False)
            assert results(db, 'parent') == []
        record = begin(db, 'old', identity)
        assert record['status'] == 'expired' and 'payload' not in record
        assert record['delivery'] == ('unknown' if delivery == 'delivering' else delivery)
    finally:
        db.close()
