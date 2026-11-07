import json
import time

import pytest

from hermes_state import SessionDB
from hermes_state_background import PREFIX, RETENTION_SECONDS, begin, complete, results, claim_delivery, pending_deliveries


def test_expired_identity_drops_input_and_output_but_rejects_reuse(tmp_path):
    with SessionDB(tmp_path / 'state.db') as db:
        db.create_session('parent', source='cli')
        binding = {'profile_id':str(tmp_path), 'parent_session_id':'parent',
            'event_type':'background.complete', 'input':'private input', 'config':'private config'}
        begin(db, 'old', binding)
        complete(db, 'old', binding, {'task_id':'old', 'text':'private output'})
        db._execute_write(lambda conn:__import__('hermes_state_background')._prune(
            conn, clock=lambda:time.time() + RETENTION_SECONDS + 1))
        with db._read_ctx() as conn:
            raw = conn.execute('SELECT value FROM state_meta WHERE key LIKE ?', (PREFIX + '%',)).fetchone()[0]
        assert 'private input' not in raw and 'private config' not in raw and 'private output' not in raw
        assert results(db, 'parent') == []
        assert pending_deliveries(db) == []
        assert not claim_delivery(db, 'old')
        assert begin(db, 'old', binding)['status'] == 'expired'
        with pytest.raises(ValueError, match='background execution unknown'):
            complete(db, 'old', binding, {'task_id':'old', 'text':'private output'})
        with pytest.raises(ValueError, match='identity'):
            begin(db, 'old', dict(binding, input='changed'))


def test_existing_expired_records_are_compacted_on_read_without_reactivation(tmp_path):
    with SessionDB(tmp_path / 'state.db') as db:
        db.create_session('parent', source='cli')
        binding = {'profile_id':str(tmp_path), 'parent_session_id':'parent', 'event_type':'background.complete'}
        begin(db, 'legacy', binding)
        def old_format(conn):
            key, raw = conn.execute('SELECT key,value FROM state_meta WHERE key LIKE ?', (PREFIX + '%',)).fetchone()
            record = json.loads(raw)
            record.update(status='expired', delivery='expired')
            record['binding']['extra'] = 'historical private input'
            conn.execute('UPDATE state_meta SET value=? WHERE key=?', (json.dumps(record), key))
        db._execute_write(old_format)
        assert results(db, 'parent') == []
        with db._read_ctx() as conn:
            raw = conn.execute('SELECT value FROM state_meta WHERE key LIKE ?', (PREFIX + '%',)).fetchone()[0]
        assert 'historical private input' not in raw
        assert begin(db, 'legacy', binding)['status'] == 'expired'


def test_identity_capacity_refuses_new_work_without_evicting_existing_fences(tmp_path, monkeypatch):
    monkeypatch.setattr('hermes_state_background.MAX_IDENTITIES', 2, raising=False)
    with SessionDB(tmp_path / 'state.db') as db:
        db.create_session('parent', source='cli')
        binding = {'profile_id':str(tmp_path), 'parent_session_id':'parent', 'event_type':'background.complete'}
        begin(db, 'one', binding)
        begin(db, 'two', binding)
        with pytest.raises(ValueError, match='background identity capacity'):
            begin(db, 'three', binding)
        assert not begin(db, 'one', binding).get('claimed')
        assert not begin(db, 'two', binding).get('claimed')
        with db._read_ctx() as conn:
            assert conn.execute('SELECT COUNT(*) FROM state_meta WHERE key LIKE ?', (PREFIX + '%',)).fetchone()[0] == 2
