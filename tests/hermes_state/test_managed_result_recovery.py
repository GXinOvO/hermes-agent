"""Identity fences and receipt compatibility for consume-only managed recovery."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from types import SimpleNamespace

import pytest

from agent.managed_worker import accept_result
from hermes_state import SessionDB
from hermes_state_runtime import (RuntimeStoreError, admit_session_input, begin_runtime_epoch,
                                 claim_session_input, mutate_worker_execution, recover_session_inputs)
from hermes_state_managed_results import PREFIX, consume_result, seal_result


@pytest.fixture(params=[False, True], ids=["direct", "compressed"])
def completed(tmp_path, monkeypatch, request):
    import hermes_state_managed_results as results
    monkeypatch.setattr(results, 'process_identity', lambda: {'pid': 2147483647, 'birth': 0})
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('logical', source='test')
    epoch = begin_runtime_epoch(db, instance_id='old')
    row = admit_session_input(db, epoch=epoch, principal_id='human', session_id='logical',
                              request_id='input', payload={'text': 'go'})
    row = claim_session_input(db, epoch=epoch, session_id='logical')
    execution = 'admission-worker:' + row['admission_id']
    db._execute_write(lambda conn: conn.execute(
        "INSERT INTO worker_executions(execution_id,session_id,kind,owner_epoch,generation,status,adoption_digest) "
        "VALUES(?,?,'compute',?,?,'running','fixture')", (execution, 'logical', epoch, row['generation'])))
    authority = SimpleNamespace(db=db, epoch=epoch, profile_id=str(tmp_path), _require_admission_open=lambda: None)
    scope = dict(epoch=epoch, profile_id=str(tmp_path), session_id='logical', execution_id=execution,
                 generation=row['generation'])
    raw = {'final_response': 'exact answer', 'prompt_tokens': 19, 'completion_tokens': 7,
           '_turn_input_tokens': 11, '_turn_output_tokens': 3, 'model': 'fixture'}
    accepted, usage = accept_result(raw)
    captured = {'result': {**accepted, 'local_viewer': True}, 'usage': usage}
    physical = 'logical'
    if request.param:
        db.end_session('logical', 'compression')
        db.create_session('tip', source='test', parent_session_id='logical')
        def rotate(conn):
            conn.execute("UPDATE sessions SET runtime_generation=1 WHERE id='tip'")
            conn.execute("UPDATE worker_executions SET session_id='tip' WHERE execution_id=?", (execution,))
            conn.execute("UPDATE session_admissions SET lineage_json=? WHERE admission_id=?",
                         (json.dumps(['logical', 'tip']), row['admission_id']))
        db._execute_write(rotate)
        physical = 'tip'
    seal_result(authority, row=row, scope=scope, raw_result=raw, captured=captured)
    kwargs = dict(epoch=epoch, execution_id=execution, session_id=physical, generation=row['generation'],
                  sequence=1, operation='execution.finish', payload={})
    assert mutate_worker_execution(db, **kwargs) == {'status': 'terminal'}
    current_epoch = begin_runtime_epoch(db, instance_id='new')
    try:
        yield db, dict(profile_id=str(tmp_path), epoch=current_epoch, admission_id=row['admission_id']), kwargs, captured
    finally:
        db.close()


def test_duplicate_readers_settle_once_with_exact_usage(completed):
    db, args, _, captured = completed
    def consume():
        with SessionDB(db.db_path) as reader:
            try:
                return consume_result(reader, **args)['status']
            except RuntimeStoreError as exc:
                return exc.reason
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: consume(), range(2)))
    assert outcomes.count('terminal') == 1, outcomes
    from gateway.session_results import admission_result
    assert admission_result(db, args['admission_id']) == captured
    assert db._read_one('SELECT COUNT(*) FROM state_meta WHERE key=?', (PREFIX + args['admission_id'],))[0] == 0


@pytest.mark.parametrize('drift', ['profile', 'epoch', 'input', 'principal', 'request', 'logical', 'physical',
                                  'generation', 'owner_epoch', 'lineage', 'config', 'revision', 'deleted',
                                  'receipt', 'unsealed', 'unfinished', 'tampered_result', 'extra_envelope', 'live_owner'])
def test_identity_drift_never_consumes_or_reexecutes(completed, drift):
    db, args, _, _ = completed
    key = PREFIX + args['admission_id']
    admission_changes = {'input': ('payload_json', '{"text":"other"}'), 'principal': ('principal_id', 'foreign'),
                         'request': ('request_id', 'other'), 'logical': ('target_session_id', 'other'),
                         'generation': ('generation', 99), 'owner_epoch': ('owner_epoch', 99),
                         'lineage': ('lineage_json', '["other"]')}
    session_changes = {'config': ('model_config', '{"model":"other"}'), 'revision': ('runtime_revision', 999)}
    if drift in admission_changes:
        column, value = admission_changes[drift]
        if drift == 'logical':
            db.create_session('other', source='test')
        db._execute_write(lambda conn: conn.execute(f'UPDATE session_admissions SET {column}=?', (value,)))
    elif drift in session_changes:
        column, value = session_changes[drift]
        db._execute_write(lambda conn: conn.execute(f'UPDATE sessions SET {column}=?', (value,)))
    else:
        handlers = {'profile': lambda: args.update(profile_id=args['profile_id'] + '-foreign'),
                    'epoch': lambda: args.update(epoch=args['epoch'] - 1),
                    'physical': lambda: _move_worker(db), 'deleted': lambda: _delete_target(db, args),
                    'receipt': lambda: db._execute_write(lambda conn: conn.execute('DELETE FROM worker_receipts')),
                    'unsealed': lambda: db._execute_write(lambda conn: conn.execute('DELETE FROM state_meta WHERE key=?', (key,)))}
        handlers.get(drift, lambda: _alter_evidence(db, key, drift))()
    with pytest.raises((RuntimeStoreError, KeyError)):
        consume_result(db, **args)
    assert db._read_one("SELECT COUNT(*) FROM session_admissions WHERE status='terminal'")[0] == 0


def _move_worker(db):
    db.create_session('other', source='test')
    db._execute_write(lambda conn: conn.execute("UPDATE worker_executions SET session_id='other'"))


def _delete_target(db, args):
    from hermes_state_runtime import resolve_unknown_session_input
    recover_session_inputs(db, epoch=args['epoch'])
    resolve_unknown_session_input(db, epoch=args['epoch'], admission_id=args['admission_id'], generation=1)
    db.delete_session('logical')


def _alter_evidence(db, key, drift):
    def change(conn):
        from hermes_state_managed_results import _proof
        value = json.loads(conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()[0])
        if drift == 'unfinished':
            value['completion'] = None
        elif drift == 'tampered_result':
            value['body']['captured']['result']['final_response'] = 'forged'
        elif drift == 'extra_envelope':
            value['untrusted'] = True
        else:
            from hermes_cli.process_identity import worker_birth
            import psutil
            value['body']['owner'] = {'pid': os.getpid(), 'birth': worker_birth(psutil.Process())}
            value['proof'] = _proof(value['body'])
        conn.execute('UPDATE state_meta SET value=? WHERE key=?', (json.dumps(value), key))
    db._execute_write(change)


def test_old_empty_finish_has_no_result_evidence(completed):
    db, args, _, _ = completed
    db._execute_write(lambda conn: conn.execute('DELETE FROM state_meta WHERE key=?', (PREFIX + args['admission_id'],)))
    assert recover_session_inputs(db, epoch=args['epoch']) == 1
    with pytest.raises(RuntimeStoreError, match='unsealed_result'):
        consume_result(db, **args)
    assert db._read_one('SELECT status FROM session_admissions')[0] == 'unknown'


def test_finish_payload_remains_closed_and_atomic(completed):
    db, args, kwargs, _ = completed
    # A forged finish cannot manufacture result evidence, even on exact old receipt replay.
    kwargs['epoch'] = args['epoch']
    kwargs['payload'] = {'result': {'final_response': 'forged'}}
    with pytest.raises(RuntimeStoreError):
        mutate_worker_execution(db, **kwargs)
    assert db._read_one('SELECT status FROM session_admissions')[0] == 'started'

@pytest.mark.parametrize('mutation', ['reset', 'rewind'])
def test_owner_mutations_block_old_sealed_result(completed, mutation):
    from hermes_state_runtime import mutate_runtime_session, resolve_unknown_session_input
    db, args, _, _ = completed
    physical = db._read_one('SELECT session_id FROM worker_executions')[0]
    db.append_messages_batch(physical, [{'role': 'user', 'content': 'old'},
                                        {'role': 'assistant', 'content': 'answer'},
                                        {'role': 'user', 'content': 'undo'}])
    target = db.get_messages(physical)[-1]['id']
    if mutation == 'reset':
        # The reset transaction's native physical-boundary invalidation, before any discard.
        db.end_session(physical, 'session_reset')
        db._execute_write(lambda conn: db._bump_conversation_generation(conn, physical, 'session_reset'))
        with pytest.raises(RuntimeStoreError):
            consume_result(db, **args)
        return
    recover_session_inputs(db, epoch=args['epoch'])
    resolve_unknown_session_input(db, epoch=args['epoch'], admission_id=args['admission_id'], generation=1)
    session = db.get_session(physical)
    payload = {'target_message_id': target}
    mutate_runtime_session(db, epoch=args['epoch'], principal_id='human', session_id=physical, request_id=mutation,
        expected_revision=session['runtime_revision'], expected_generation=session['runtime_generation'],
        operation=mutation, payload=payload)
    with pytest.raises(RuntimeStoreError):
        consume_result(db, **args)
    from gateway.session_results import admission_result
    assert admission_result(db, args['admission_id']) is None


@pytest.mark.parametrize('payload', [{'result': {'final_response': 'forged'}}, {'completion': True}])
def test_nonempty_finish_cannot_create_completion_evidence(tmp_path, payload):
    with SessionDB(tmp_path / 'state.db') as db:
        db.create_session('s', source='test')
        epoch = begin_runtime_epoch(db, instance_id='owner')
        db._execute_write(lambda conn: conn.execute(
            "INSERT INTO worker_executions(execution_id,session_id,kind,owner_epoch,generation,status,adoption_digest) "
            "VALUES('worker','s','compute',?,0,'running','fixture')", (epoch,)))
        with pytest.raises(RuntimeStoreError, match='invalid_params'):
            mutate_worker_execution(db, epoch=epoch, execution_id='worker', session_id='s', generation=0,
                                    sequence=1, operation='execution.finish', payload=payload)
        assert db._read_one('SELECT status FROM worker_executions')[0] == 'running'
        assert db._read_one('SELECT COUNT(*) FROM worker_receipts')[0] == 0

