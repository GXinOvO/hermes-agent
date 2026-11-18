import time
from unittest.mock import patch

import pytest

from hermes_state import SessionDB


def test_completed_result_is_read_by_fresh_owner_without_reexecution(tmp_path):
    from hermes_state_background import begin, complete, results
    path = tmp_path / 'state.db'
    binding = {'parent_session_id':'parent','profile_id':str(tmp_path),'event_type':'background.complete',
               'prompt':'task','cwd':str(tmp_path)}
    db = SessionDB(path)
    try:
        db.create_session('parent', source='cli')
        assert begin(db, 'bg_one', binding)['status'] == 'running'
        complete(db, 'bg_one', binding, {'task_id':'bg_one','text':'accepted result'})
    finally:
        db.close()
    reopened = SessionDB(path)
    try:
        saved = results(reopened, 'parent')
        assert saved == [{'event_type':'background.complete','payload':{'task_id':'bg_one','text':'accepted result'}}]
        assert begin(reopened, 'bg_one', binding)['status'] == 'completed'
        assert results(reopened, 'parent') == saved
        assert results(reopened, 'foreign') == []
        assert reopened.get_messages('parent') == []
    finally:
        reopened.close()


def test_unknown_owner_and_binding_or_result_drift_never_authorize_replay(tmp_path):
    from hermes_state_background import begin, complete, results, claim_delivery, finish_delivery
    db = SessionDB(tmp_path / 'state.db')
    binding = {'parent_session_id':'parent','profile_id':str(tmp_path),'event_type':'background.complete',
               'prompt':'task','cwd':str(tmp_path)}
    try:
        db.create_session('parent', source='cli')
        begin(db, 'bg_one', binding)
        with pytest.raises(ValueError, match='identity'):
            begin(db, 'bg_one', dict(binding, prompt='other'))
        assert begin(db, 'bg_one', binding)['status'] == 'running'
        with patch('hermes_state_background._owner_live', return_value=False):
            assert begin(db, 'bg_one', binding)['status'] == 'unknown'
            assert results(db, 'parent')[0]['payload']['text'].startswith('error: unknown_execution')
        with pytest.raises(ValueError, match='unknown'):
            complete(db, 'bg_one', binding, {'task_id':'bg_one','text':'late result'})
        begin(db, 'bg_two', binding)
        complete(db, 'bg_two', binding, {'task_id':'bg_two','text':'sealed'})
        with pytest.raises(ValueError, match='result'):
            complete(db, 'bg_two', binding, {'task_id':'bg_two','text':'changed'})
        assert claim_delivery(db, 'bg_two') is True
        assert claim_delivery(db, 'bg_two') is False
        finish_delivery(db, 'bg_two', 'unknown')
        assert claim_delivery(db, 'bg_two') is False
        assert results(db, 'parent')[-1]['payload']['text'] == 'sealed'
    finally:
        db.close()


def test_retired_owner_cannot_seal_and_deleted_session_does_not_replay(tmp_path):
    from hermes_state_background import begin, complete, results
    from hermes_state_runtime import begin_runtime_epoch, RuntimeStoreError
    db = SessionDB(tmp_path / 'state.db')
    try:
        db.create_session('parent', source='cli')
        epoch = begin_runtime_epoch(db, instance_id='owner-one')
        binding = {'parent_session_id':'parent', 'profile_id':str(tmp_path),
                   'event_type':'background.complete', 'owner_epoch':epoch}
        begin(db, 'bg_one', binding)
        complete(db, 'bg_one', binding, {'task_id':'bg_one', 'text':'sealed'})
        begin(db, 'bg_two', binding)
        begin_runtime_epoch(db, instance_id='owner-two')
        with pytest.raises(RuntimeStoreError, match='stale_epoch'):
            complete(db, 'bg_two', binding, {'task_id':'bg_two', 'text':'late'})
        assert results(db, 'parent')[0]['payload']['text'] == 'sealed'
        db._execute_write(lambda conn: conn.execute('DELETE FROM sessions WHERE id=?', ('parent',)))
        assert results(db, 'parent') == []
    finally:
        db.close()


def test_delivery_claim_has_its_own_process_fence(tmp_path):
    import os
    from hermes_state_background import begin, complete, claim_delivery, finish_delivery
    db = SessionDB(tmp_path / 'state.db')
    binding = {'parent_session_id':'parent', 'profile_id':str(tmp_path), 'event_type':'background.complete'}
    try:
        db.create_session('parent', source='cli')
        begin(db, 'bg_one', binding)
        complete(db, 'bg_one', binding, {'task_id':'bg_one', 'text':'sealed'})
        assert claim_delivery(db, 'bg_one')
        with patch('hermes_state_background.os.getpid', return_value=os.getpid() + 100000):
            with pytest.raises(ValueError, match='delivery owner'):
                finish_delivery(db, 'bg_one', 'delivered')
        finish_delivery(db, 'bg_one', 'unknown')
        assert not claim_delivery(db, 'bg_one')
    finally:
        db.close()


def test_retention_expires_identities_without_allow_replay(tmp_path):
    from hermes_state_background import begin, complete, results, claim_delivery, finish_delivery
    db = SessionDB(tmp_path / 'state.db')
    binding = {'parent_session_id':'parent', 'profile_id':str(tmp_path), 'event_type':'background.complete'}
    try:
        db.create_session('parent', source='cli')
        now = time.time()
        with patch('hermes_state_background.time.time', return_value=now):
            begin(db, 'bg_old', binding)
        complete(db, 'bg_old', binding, {'task_id':'bg_old', 'text':'old'})
        with patch('hermes_state_background.time.time', return_value=now + 1):
            begin(db, 'bg_new', binding)
        complete(db, 'bg_new', binding, {'task_id':'bg_new', 'text':'new'})
        with patch('hermes_state_background.time.time', return_value=now + 2):
            assert begin(db, 'bg_newer', binding)['status'] == 'running'
        complete(db, 'bg_newer', binding, {'task_id':'bg_newer', 'text':'newer'})
        assert [r['payload']['text'] for r in results(db, 'parent')] == ['old', 'new', 'newer']
        for task in ('bg_old', 'bg_new', 'bg_newer'):
            assert claim_delivery(db, task)
            finish_delivery(db, task, 'delivered')
        with patch('hermes_state_background.MAX_RESULTS', 1):
            db._execute_write(lambda conn: __import__('hermes_state_background')._prune(conn))
        assert [r['payload']['text'] for r in results(db, 'parent')] == ['newer']
        assert begin(db, 'bg_old', binding)['status'] == 'expired'
        assert begin(db, 'bg_new', binding)['status'] == 'expired'
    finally:
        db.close()
