"""Lossless accepted inline seals under new-execution admission pressure."""
import asyncio
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agent.managed_worker import MAX_FRAME, accept_result, encode_frame
from gateway import session_managed_worker as managed
from gateway.managed_result_recovery import recover_completed_managed_results
from gateway.session_contract import SessionActor, SessionRef, Submission
from gateway.session_results import admission_result
from hermes_state import SessionDB
import hermes_state_managed_result_capacity as capacity
import hermes_state_managed_results as results
from hermes_state_runtime import (
    RuntimeStoreError, begin_runtime_epoch, mutate_worker_execution,
    claim_session_input,
)
from tests.gateway import test_managed_result_capacity as fixtures
from tests.waiting import async_wait_until


ACTOR = SessionActor('human', 'owned', frozenset({'session:submit', 'session:control'}), 'cli')


def seal_bytes(db):
    return db._read_one('SELECT COALESCE(SUM(length(CAST(value AS BLOB))),0) '
                        'FROM state_meta WHERE key LIKE ?', (results.PREFIX + '%',))[0]


def native_overflow_recovery(tmp_path, boundary):
    from tests.gateway.fixtures.local_recovery_probe import child_env, daemon, rpc, websocket
    root = Path(__file__).resolve().parents[2]
    home, user = tmp_path / 'state', tmp_path / 'user'
    home.mkdir()
    user.mkdir()
    marker, finished = tmp_path / 'runner-calls', tmp_path / 'finished'
    (home / 'config.yaml').write_text(json.dumps({
        'gateway': {'multiplex_profiles': False, 'managed_workers': True},
        'model': {'provider': 'custom', 'default': 'inert', 'base_url': 'http://127.0.0.1:1/v1'},
        'auxiliary': {'title_generation': {'enabled': False}}, 'platform_toolsets': {'cli': []}}))
    env = child_env()
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home), PYTHONPATH=str(root),
               OVERFLOW_CASE=boundary, OVERFLOW_RUNNER=str(marker), OVERFLOW_FINISHED=str(finished))

    def query(sql, args=()):
        with closing(sqlite3.connect(f'file:{home / "state.db"}?mode=ro', uri=True)) as conn:
            return conn.execute(sql, args).fetchall()

    async def first(desc, owner):
        async with websocket(home, desc) as ws:
            created = await rpc(ws, 'session.create', request_id='inert', source='cli', cwd=str(home),
                                model='inert', provider='custom', base_url='http://127.0.0.1:1/v1',
                                api_key='inert-only', toolsets=[], ignore_rules=True)
            assert 'result' in created, created
            sid = created['result']['session_id']
            submitted = await rpc(ws, 'prompt.submit', session_id=sid, input_id='overflow', text='inert')
            assert 'result' in submitted, submitted
            admission = submitted['result']['admission_id']
            await async_wait_until(lambda: finished.exists() or query(
                'SELECT status FROM session_admissions WHERE admission_id=?', (admission,)) in [[('unknown',)], [('terminal',)]],
                timeout=60, what='accepted output sealed and authenticated finish committed')
            status = query('SELECT status FROM session_admissions WHERE admission_id=?', (admission,))
            assert finished.exists(), {'status': status, 'runner_calls': marker.read_text() if marker.exists() else ''}
            info = json.loads(finished.read_text())
            assert status == [('started',)]
            assert info['completion']['sequence'] == 1
            assert info['finish_receipt'] == {'status': 'terminal'}
            assert info['seal_bytes'] > info['threshold']
            assert info['finished_bytes'] > info['seal_bytes']
            assert info['wire_bytes'] < MAX_FRAME
            # New work must be refused while the accepted overflow remains pending.
            other = await rpc(ws, 'session.create', request_id='pressure', source='cli', cwd=str(home),
                              model='inert', provider='custom', base_url='http://127.0.0.1:1/v1',
                              api_key='inert-only', toolsets=[], ignore_rules=True)
            assert 'result' in other, other
            refused = await rpc(ws, 'prompt.submit', session_id=other['result']['session_id'],
                                input_id='new-pressure', text='never execute')
            refused_id = refused['result']['admission_id']
            await async_wait_until(lambda: query('SELECT status FROM session_admissions WHERE admission_id=?',
                                                (refused_id,)) == [('terminal',)],
                                   timeout=20, what='new execution refused before runner')
            assert marker.read_text().splitlines() == ['runner']
            assert query('SELECT COUNT(*) FROM worker_executions WHERE execution_id=?',
                         ('admission-worker:' + refused_id,)) == [(0,)]
            rejected = await rpc(ws, 'prompt.receipt', session_id=other['result']['session_id'],
                                 admission_id=refused_id, include_result=True)
            assert rejected['result']['result']['failure_reason'] == 'managed_result_capacity'
            owner.kill()
            await asyncio.to_thread(owner.wait, 10)
            return sid, admission, info

    expected = '«redacted-vault-secret»' * (3 * 1024 * 1024) if boundary == 'single_result_over_64MiB' else 'EXACT_ACCEPTED'
    expected_hash = hashlib.sha256(expected.encode()).hexdigest()

    async def read_back(desc, sid, admission):
        async with websocket(home, desc, max_size=128 * 1024 * 1024) as ws:
            reply = await rpc(ws, 'prompt.receipt', session_id=sid, admission_id=admission, include_result=True)
            saved = reply.get('result', {})
            assert saved.get('status') == 'terminal' and saved.get('outcome') == 'completed', {
                k: v for k, v in saved.items() if k != 'result'}
            text = saved['result']['final_response']
            assert text == expected
            assert hashlib.sha256(text.encode()).hexdigest() == expected_hash
            # Consume-only startup has retired the exact pending key.
            assert query('SELECT COUNT(*) FROM state_meta WHERE key=?', (results.PREFIX + admission,)) == [(0,)]
            return saved['result']

    with daemon(root, home, env, barrier=True, fixture=str(Path(__file__).resolve())) as (owner, desc):
        sid, admission, info = asyncio.run(first(desc, owner))
        old_epoch = desc['authority_epoch']
    with daemon(root, home, env, barrier=False) as (_, desc):
        assert desc['authority_epoch'] > old_epoch
        saved = asyncio.run(read_back(desc, sid, admission))
    with daemon(root, home, env, barrier=False) as (_, desc):
        assert asyncio.run(read_back(desc, sid, admission)) == saved
    assert marker.read_text().splitlines() == ['runner']
    print(json.dumps({'boundary': boundary, 'runner_calls': 1, 'model_calls': 0, 'restarts': 2,
                      'exact_utf8_bytes': len(expected.encode()), 'exact_sha256': expected_hash, **info}))


@pytest.mark.asyncio
async def test_concurrent_accepted_turns_overflow_then_new_ingress_refuses(tmp_path, monkeypatch):
    from gateway.session_authority import LiveSession
    gate = tmp_path / 'release'
    db, authority, ref, marker, spawned = fixtures.inert_ingress(tmp_path, monkeypatch, gate=gate)
    finished, settle = set(), asyncio.Event()

    async def hold_after_finish(owner, sid, accepted):
        row = db._read_one("SELECT * FROM session_admissions WHERE target_session_id=? AND status='started'", (sid,))
        mutate_worker_execution(db, epoch=owner.epoch, execution_id='admission-worker:' + row['admission_id'],
                                session_id=sid, generation=row['generation'], sequence=1,
                                operation='execution.finish', payload={})
        finished.add(sid)
        await settle.wait()

    monkeypatch.setattr(managed, '_finalize_managed_refusal', hold_after_finish)
    with db:
        fixtures.legal_seal(db, authority, 'held')
        monkeypatch.setattr(results, 'MAX_BYTES', seal_bytes(db) + 1)
        for sid in ('t', 'new'):
            db.create_session(sid, source='test')
            authority.sessions[sid] = LiveSession(authority.sessions['s'].source, 'route-' + sid)
        try:
            admitted = await asyncio.gather(
                authority.submit(ACTOR, Submission('first', ref, {'text': 'first'}, 'queue')),
                authority.submit(ACTOR, Submission('second', SessionRef('owned', 't'), {'text': 'second'}, 'queue')))
            await async_wait_until(lambda: marker.exists() and len(marker.read_text().splitlines()) == 2,
                                   timeout=30, what='two accepted workers before byte growth')
            assert db._read_one('SELECT COUNT(*) FROM state_meta WHERE key LIKE ?', (capacity.PREFIX + '%',))[0] == 2
            gate.touch()
            await async_wait_until(lambda: finished == {'s', 't'}, timeout=30, what='both accepted seals and finishes')
            used = seal_bytes(db)
            assert used > results.MAX_BYTES
            assert db._read_one('SELECT COUNT(*) FROM state_meta WHERE key LIKE ?', (results.PREFIX + '%',))[0] == 3
            assert db._read_one('SELECT COUNT(*) FROM state_meta WHERE key LIKE ?', (capacity.PREFIX + '%',))[0] == 0
            refused = await authority.submit(ACTOR, Submission('new', SessionRef('owned', 'new'), {'text': 'new'}, 'queue'))
            await asyncio.wait_for(authority.sessions['new'].task, 20)
            assert admission_result(db, refused.admission_id)['result']['failure_reason'] == 'managed_result_capacity'
            assert len(spawned) == 2 and len(marker.read_text().splitlines()) == 2
            settle.set()
            await asyncio.wait_for(asyncio.gather(*(authority.sessions[s].task for s in ('s', 't'))), 30)
            for receipt in admitted:
                assert admission_result(db, receipt.admission_id)['result']['final_response'] == 'EXACT_ACCEPTED'
            assert seal_bytes(db) < used
            print(json.dumps({'concurrent_accepted': 2, 'pending_bytes': used, 'threshold': results.MAX_BYTES,
                              'logical_seals': 3, 'new_runner_calls': 0}))
        finally:
            gate.touch()
            settle.set()
            await asyncio.gather(*(authority.sessions[s].task for s in ('s', 't', 'new') if authority.sessions[s].task),
                                 return_exceptions=True)


@pytest.mark.parametrize('stop_cause', ['Gateway restarting', 'Gateway shutting down'])
def test_overflow_keeps_interruption_cause_through_finish_and_fresh_owner(tmp_path, monkeypatch, stop_cause):
    monkeypatch.setattr(results, 'process_identity', lambda: {'pid': 2147483647, 'birth': 0})
    monkeypatch.setattr(results, 'MAX_BYTES', 1)
    with SessionDB(tmp_path / 'state.db') as db:
        authority = owner(db)
        row, scope = executing(db, authority, 'stopped')
        raw = {'final_response': '', 'interrupted': True, 'completed': False}
        accepted, usage = accept_result(raw)
        results.seal_result(authority, row=row, scope=scope, raw_result=raw,
                            captured={'result': {**accepted, 'local_viewer': True}, 'usage': usage},
                            stop_cause=stop_cause)
        mutate_worker_execution(db, epoch=authority.epoch, execution_id=scope['execution_id'], session_id='stopped',
                                generation=row['generation'], sequence=1, operation='execution.finish', payload={})
        epoch = begin_runtime_epoch(db, instance_id='fresh')
        assert recover_completed_managed_results(db, profile_id='owned', epoch=epoch) == 1
        saved = admission_result(db, row['admission_id'])
        assert saved['result']['interrupt_message'] == stop_cause
        assert get_outcome(db, row['admission_id']) == 'interrupted'
        assert db._read_one('SELECT key FROM state_meta WHERE key=?', (results.PREFIX + row['admission_id'],)) is None


def get_outcome(db, admission_id):
    return db._read_one('SELECT outcome FROM session_admissions WHERE admission_id=?', (admission_id,))[0]


def owner(db):
    return SimpleNamespace(db=db, epoch=begin_runtime_epoch(db, instance_id='owner'), profile_id='owned',
                           _require_admission_open=lambda: None)


def executing(db, authority, name):
    row = fixtures.claimed(db, authority, name)
    execution = 'admission-worker:' + row['admission_id']
    db._execute_write(lambda conn: conn.execute(
        "INSERT INTO worker_executions(execution_id,session_id,kind,owner_epoch,generation,status,adoption_digest) "
        "VALUES(?,?,'compute',?,?,'running','fixture')", (execution, name, authority.epoch, row['generation'])))
    return row, dict(epoch=authority.epoch, profile_id='owned', session_id=name,
                     execution_id=execution, generation=row['generation'])


@pytest.mark.parametrize('drift', ['profile', 'generation', 'execution', 'epoch', 'lost'])
def test_overflow_never_bypasses_producer_authority(tmp_path, monkeypatch, drift):
    from gateway.session_worker_reservation import lose_admission_worker
    monkeypatch.setattr(results, 'MAX_BYTES', 1)
    with SessionDB(tmp_path / 'state.db') as db:
        authority = owner(db)
        row, scope = executing(db, authority, 'owned')
        if drift == 'lost':
            lose_admission_worker(authority, row, scope)
        elif drift == 'epoch':
            begin_runtime_epoch(db, instance_id='fresh')
        else:
            field, value = {'profile': ('profile_id', 'foreign'), 'generation': ('generation', 999),
                            'execution': ('execution_id', 'other')}[drift]
            scope[field] = value
        raw = {'final_response': 'UNTRUSTED'}
        accepted, usage = accept_result(raw)
        with pytest.raises(RuntimeStoreError):
            results.seal_result(authority, row=row, scope=scope, raw_result=raw,
                                captured={'result': {**accepted, 'local_viewer': True}, 'usage': usage})
        assert db._read_one('SELECT COUNT(*) FROM state_meta WHERE key LIKE ?', (results.PREFIX + '%',))[0] == 0


def test_sqlite_save_failure_remains_explicit_and_preserves_slot(tmp_path, monkeypatch):
    with SessionDB(tmp_path / 'state.db') as db:
        authority = owner(db)
        row, scope = executing(db, authority, 'full-device')
        capacity.reserve_capacity(authority, row['admission_id'])
        monkeypatch.setattr(results, 'MAX_BYTES', 1)
        db._execute_write(lambda conn: conn.execute(
            "CREATE TRIGGER injected_save_failure BEFORE INSERT ON state_meta "
            "WHEN NEW.key LIKE 'managed.result.v1:%' BEGIN SELECT RAISE(ABORT,'injected storage failure'); END"))
        raw = {'final_response': 'ACCEPTED'}
        accepted, usage = accept_result(raw)
        with pytest.raises(sqlite3.IntegrityError, match='injected storage failure'):
            results.seal_result(authority, row=row, scope=scope, raw_result=raw,
                                captured={'result': {**accepted, 'local_viewer': True}, 'usage': usage})
        assert db._read_one('SELECT COUNT(*) FROM state_meta WHERE key LIKE ?', (results.PREFIX + '%',))[0] == 0
        assert db._read_one('SELECT key FROM state_meta WHERE key=?', (capacity.PREFIX + row['admission_id'],)) is not None
        assert get_outcome(db, row['admission_id']) is None


@pytest.mark.parametrize('slot', [False, True], ids=['legacy-no-slot', 'current-slot'])
def test_already_executing_workers_seal_finish_and_reopen_over_full_pressure(tmp_path, monkeypatch, slot):
    monkeypatch.setattr(results, 'process_identity', lambda: {'pid': 2147483647, 'birth': 0})
    path = tmp_path / 'state.db'
    with SessionDB(path) as db:
        authority = owner(db)
        # The first seal does not decide whether an already-running second worker may finish.
        fixtures.legal_seal(db, authority, 'held')
        row = fixtures.claimed(db, authority, 'accepted')
        if slot:
            capacity.reserve_capacity(authority, row['admission_id'])
        execution = 'admission-worker:' + row['admission_id']
        db._execute_write(lambda conn: conn.execute(
            "INSERT INTO worker_executions(execution_id,session_id,kind,owner_epoch,generation,status,adoption_digest) "
            "VALUES(?,'accepted','compute',?,?,'running','fixture')", (execution, authority.epoch, row['generation'])))
        monkeypatch.setattr(results, 'MAX_BYTES', seal_bytes(db))
        monkeypatch.setattr(results, 'MAX_ROWS', 1)
        follower = fixtures.claimed(db, authority, 'follower')
        with pytest.raises(RuntimeStoreError, match='managed_result_capacity'):
            capacity.reserve_capacity(authority, follower['admission_id'])
        raw = {'final_response': 'EXACT_OLD_ACCEPTED', '_turn_input_tokens': 2}
        accepted, usage = accept_result(raw)
        scope = dict(epoch=authority.epoch, profile_id='owned', session_id='accepted',
                     execution_id=execution, generation=row['generation'])
        results.seal_result(authority, row=row, scope=scope, raw_result=raw,
                            captured={'result': {**accepted, 'local_viewer': True}, 'usage': usage})
        pending_size = seal_bytes(db)
        assert pending_size > results.MAX_BYTES
        assert db._read_one('SELECT COUNT(*) FROM state_meta WHERE key LIKE ?', (results.PREFIX + '%',))[0] == 2
        assert db._read_one('SELECT COUNT(*) FROM state_meta WHERE key LIKE ?', (capacity.PREFIX + '%',))[0] == 0
        mutate_worker_execution(db, epoch=authority.epoch, execution_id=execution, session_id='accepted',
                                generation=row['generation'], sequence=1, operation='execution.finish', payload={})
        assert seal_bytes(db) > pending_size
    with SessionDB(path) as db:
        epoch = begin_runtime_epoch(db, instance_id='fresh')
        assert recover_completed_managed_results(db, profile_id='owned', epoch=epoch) == 1
        assert admission_result(db, row['admission_id'])['result']['final_response'] == raw['final_response']
        assert recover_completed_managed_results(db, profile_id='owned', epoch=epoch) == 0


@pytest.mark.parametrize('damage', ['proof', 'missing', 'unfinished', 'profile', 'live-owner', 'expired'])
def test_overflow_proof_owner_ttl_and_good_sibling_recovery(tmp_path, monkeypatch, damage):
    monkeypatch.setattr(results, 'process_identity', lambda: {'pid': 2147483647, 'birth': 0})
    monkeypatch.setattr(results, 'MAX_BYTES', 1)
    with SessionDB(tmp_path / 'state.db') as db:
        authority = owner(db)
        bad, _ = fixtures.legal_seal(db, authority, 'bad', text='UNCERTAIN', finished=damage != 'unfinished')
        good, _ = fixtures.legal_seal(db, authority, 'good', text='EXACT_GOOD', finished=True)
        key = results.PREFIX + bad['admission_id']
        raw = db._read_one('SELECT value FROM state_meta WHERE key=?', (key,))[0]
        value = json.loads(raw)
        if damage == 'missing':
            db._execute_write(lambda conn: conn.execute('DELETE FROM state_meta WHERE key=?', (key,)))
        else:
            from hermes_cli.process_identity import worker_birth
            import psutil
            mutations = {
                'proof': lambda: value['body']['captured']['result'].update(final_response='FORGED'),
                'profile': lambda: value['body'].update(profile_id='foreign'),
                'live-owner': lambda: value['body'].update(owner={'pid': os.getpid(), 'birth': worker_birth(psutil.Process())}),
                'expired': lambda: value['body'].update(created_at=value['body']['created_at'] - results.RETENTION_SECONDS - 1),
                'unfinished': lambda: None,
            }
            mutations[damage]()
            if damage != 'proof':
                value['proof'] = results._proof(value['body'])
            raw = json.dumps(value)
            db._execute_write(lambda conn: conn.execute('UPDATE state_meta SET value=? WHERE key=?', (raw, key)))
        epoch = begin_runtime_epoch(db, instance_id='fresh')
        assert recover_completed_managed_results(db, profile_id='owned', epoch=epoch) == 1
        assert admission_result(db, good['admission_id'])['result']['final_response'] == 'EXACT_GOOD'
        assert admission_result(db, bad['admission_id']) is None
        assert recover_completed_managed_results(db, profile_id='owned', epoch=epoch) == 0
        left = db._read_one('SELECT value FROM state_meta WHERE key=?', (key,))
        if damage in ('missing', 'expired'):
            assert left is None
        else:
            assert left[0] == raw
        if damage == 'expired':
            with pytest.raises(RuntimeStoreError, match='unknown_execution'):
                claim_session_input(db, epoch=epoch, session_id='bad')


def install_daemon_fixture():
    from agent import redact
    script = fixtures.CHILD
    if os.environ['OVERFLOW_CASE'] == 'single_result_over_64MiB':
        script = script.replace("'EXACT_ACCEPTED'", "'q' * (3 * 1024 * 1024)")
    popen = subprocess.Popen

    class InertChild(popen):
        def __init__(self, args, **kwargs):
            if args[-2:] == ['-m', 'agent.managed_worker']:
                args = [sys.executable, '-c', script, os.environ['OVERFLOW_RUNNER'], 'native']
            super().__init__(args, **kwargs)

    managed.subprocess.Popen = InertChild
    reserve, seal = capacity.reserve_capacity, results.seal_result
    observed = {}

    def reserve_with_pressure(authority, admission_id):
        if os.environ['OVERFLOW_CASE'] == 'remaining_1_byte' and not observed.get('prepared'):
            observed['prepared'] = True
            fixtures.legal_seal(authority.db, authority, 'held')
            results.MAX_BYTES = seal_bytes(authority.db) + 1
        return reserve(authority, admission_id)

    def capture_seal(authority, **kw):
        if kw['row']['request_id'] != 'overflow':
            return seal(authority, **kw)
        if os.environ['OVERFLOW_CASE'] == 'single_result_over_64MiB':
            redact.register_vault_redaction_value('q')
        result = seal(authority, **kw)
        value = authority.db._read_one('SELECT value FROM state_meta WHERE key=?',
                                      (results.PREFIX + kw['row']['admission_id'],))[0]
        observed.update(seal_bytes=len(value.encode()), threshold=results.MAX_BYTES,
                        wire_bytes=len(encode_frame({'type': 'result', 'result': kw['raw_result']})))
        return result

    async def after_finish(authority, session_id, accepted):
        row = authority.db._read_one("SELECT * FROM session_admissions WHERE request_id='overflow'")
        execution = 'admission-worker:' + row['admission_id']
        encoded = authority.db._read_one('SELECT value FROM state_meta WHERE key=?',
                                        (results.PREFIX + row['admission_id'],))[0]
        value = json.loads(encoded)
        receipt = authority.db._read_one('SELECT result_json FROM worker_receipts WHERE execution_id=? AND sequence=1',
                                        (execution,))[0]
        info = {**observed, 'finished_bytes': len(encoded.encode()), 'completion': value['completion'],
                'finish_receipt': json.loads(receipt), 'pending_bytes': seal_bytes(authority.db),
                'logical_seals': authority.db._read_one('SELECT COUNT(*) FROM state_meta WHERE key LIKE ?',
                                                       (results.PREFIX + '%',))[0]}
        Path(os.environ['OVERFLOW_FINISHED']).write_text(json.dumps(info))
        await asyncio.Future()

    capacity.reserve_capacity = reserve_with_pressure
    results.seal_result = capture_seal
    managed._finalize_managed_refusal = after_finish


if __name__ == '__main__':
    import runpy
    install_daemon_fixture()
    runpy.run_module('gateway.run', run_name='__main__')
