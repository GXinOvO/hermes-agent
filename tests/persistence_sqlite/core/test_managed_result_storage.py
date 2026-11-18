import json
from types import SimpleNamespace

import pytest

from agent.managed_worker import accept_result
from gateway.session_results import admission_result
from hermes_state import SessionDB
from hermes_state_managed_results import PREFIX, consume_result, seal_result
from hermes_state_runtime import admit_session_input, begin_runtime_epoch, claim_session_input, mutate_worker_execution


def sealed(tmp_path, monkeypatch, raw, *, stop_cause=None, before=None):
    import hermes_state_managed_results as results
    monkeypatch.setattr(results, 'process_identity', lambda:{'pid':2147483647, 'birth':0})
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('s', source='cli')
    if before:
        before(db)
    epoch = begin_runtime_epoch(db, instance_id='old')
    admit_session_input(db, epoch=epoch, principal_id='human', session_id='s',
        request_id='request', payload={'text':'input value'})
    row = claim_session_input(db, epoch=epoch, session_id='s')
    execution = 'admission-worker:' + row['admission_id']
    db._execute_write(lambda conn:conn.execute("INSERT INTO worker_executions"
        "(execution_id,session_id,kind,owner_epoch,generation,status,adoption_digest) "
        "VALUES(?,'s','compute',?,?,'running','fixture')", (execution,epoch,row['generation'])))
    authority = SimpleNamespace(db=db, epoch=epoch, profile_id=str(tmp_path), _require_admission_open=lambda:None)
    scope = dict(epoch=epoch, profile_id=str(tmp_path), session_id='s', execution_id=execution, generation=row['generation'])
    accepted, usage = accept_result(raw)
    captured = {'result':{**accepted, 'local_viewer':True}, 'usage':usage}
    kwargs = {'stop_cause':stop_cause} if stop_cause else {}
    seal_result(authority, row=row, scope=scope, raw_result=raw, captured=captured, **kwargs)
    mutate_worker_execution(db, epoch=epoch, execution_id=execution, session_id='s', generation=row['generation'],
        sequence=1, operation='execution.finish', payload={})
    return db, row, begin_runtime_epoch(db, instance_id='new')


def test_managed_pending_copy_uses_terminal_redaction_and_does_not_duplicate_input(tmp_path, monkeypatch):
    monkeypatch.setattr('agent.redact._REDACT_ENABLED', True)
    monkeypatch.delenv('HERMES_REDACT_SECRETS', raising=False)
    token = 'glpat-' + 'AbCdEf01234567890123456789'
    reply = 'TOKEN=' + token
    db, row, epoch = sealed(tmp_path, monkeypatch, {'final_response':reply})
    try:
        with db._read_ctx() as conn:
            raw = conn.execute('SELECT value FROM state_meta WHERE key=?', (PREFIX + row['admission_id'],)).fetchone()[0]
        value = json.loads(raw)
        assert token not in raw
        assert 'raw_result' not in value['body']
        assert 'payload_json' not in value['body']['admission']
        assert 'input value' not in raw
        assert consume_result(db, profile_id=str(tmp_path), epoch=epoch, admission_id=row['admission_id'])['outcome'] == 'completed'
        assert token not in json.dumps(admission_result(db, row['admission_id']))
    finally:
        db.close()


@pytest.mark.parametrize('cause', ['Gateway restarting', 'Gateway shutting down'])
def test_interrupted_managed_result_keeps_owner_stop_cause_across_recovery(tmp_path, monkeypatch, cause):
    db, row, epoch = sealed(tmp_path, monkeypatch, {'final_response':'', 'interrupted':True, 'completed':False}, stop_cause=cause)
    try:
        result = consume_result(db, profile_id=str(tmp_path), epoch=epoch, admission_id=row['admission_id'])
        assert result['outcome'] == 'interrupted'
        assert admission_result(db, row['admission_id'])['result']['interrupt_message'] == cause
        assert len(db.get_messages('s')) == 1
    finally:
        db.close()


def test_safety_refused_result_closes_original_turn_in_the_settlement_transaction(tmp_path, monkeypatch):
    boundary = []
    def history(db):
        db.append_messages_batch('s', [{'role':'user', 'content':'refused input'},
                                       {'role':'assistant', 'content':'safety refusal'}])
        boundary.append(db.get_messages('s')[0]['id'])
    raw = {'final_response':'refused', 'failed':True, 'failure_reason':'content_policy_blocked'}
    db, row, epoch = sealed(tmp_path, monkeypatch, raw, before=history)
    try:
        result = consume_result(db, profile_id=str(tmp_path), epoch=epoch, admission_id=row['admission_id'])
        assert result['outcome'] == 'failed'
        assert db.get_messages('s') == []
        with db._read_ctx() as conn:
            display = conn.execute('SELECT active,compacted,display_kind FROM messages WHERE id=?', (boundary[0],)).fetchone()
        assert tuple(display) == (0, 1, 'content_refused')
    finally:
        db.close()


def test_expired_orphan_seal_releases_capacity_but_does_not_authorize_reexecution(tmp_path, monkeypatch):
    from gateway.managed_result_recovery import recover_completed_managed_results
    from hermes_state_runtime import RuntimeStoreError
    monkeypatch.setattr('time.time', lambda:1000.0)
    db, row, epoch = sealed(tmp_path, monkeypatch, {'final_response':'finished'})
    try:
        monkeypatch.setattr('time.time', lambda:1001.0 + 7 * 24 * 3600)
        assert recover_completed_managed_results(db, profile_id=str(tmp_path), epoch=epoch) == 0
        assert db._read_one('SELECT COUNT(*) FROM state_meta WHERE key=?', (PREFIX + row['admission_id'],))[0] == 0
        assert db._read_one('SELECT status FROM session_admissions WHERE admission_id=?', (row['admission_id'],))[0] == 'unknown'
        with pytest.raises(RuntimeStoreError, match='unknown_execution'):
            claim_session_input(db, epoch=epoch, session_id='s')
        assert admission_result(db, row['admission_id']) is None
    finally:
        db.close()


@pytest.mark.parametrize('damaged', ['not json', '[]', '{"body": []}'])
def test_malformed_orphan_does_not_block_valid_startup_recovery(tmp_path, monkeypatch, damaged):
    from gateway.managed_result_recovery import recover_completed_managed_results
    db, row, epoch = sealed(tmp_path, monkeypatch, {'final_response':'valid result'})
    try:
        db._execute_write(lambda conn:conn.execute('INSERT INTO state_meta VALUES(?,?)', (PREFIX + 'damaged', damaged)))
        assert recover_completed_managed_results(db, profile_id=str(tmp_path), epoch=epoch) == 1
        assert admission_result(db, row['admission_id'])['result']['final_response'] == 'valid result'
        assert recover_completed_managed_results(db, profile_id=str(tmp_path), epoch=epoch) == 0
        with db._read_ctx() as conn:
            assert conn.execute('SELECT value FROM state_meta WHERE key=?', (PREFIX + 'damaged',)).fetchone()[0] == damaged
    finally:
        db.close()
