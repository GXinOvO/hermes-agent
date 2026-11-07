"""Managed ingress refuses exhausted retention before starting an inert runner."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from agent.managed_worker import accept_result
from agent.managed_worker import MAX_FRAME, encode_frame
from gateway import session_managed_worker as managed
from gateway.session_contract import SessionActor, SessionRef, Submission
from gateway.session_results import admission_result
from hermes_state import SessionDB
import hermes_state_managed_results as results
from hermes_state_runtime import (
    RuntimeStoreError, admit_session_input, begin_runtime_epoch, claim_session_input,
    get_session_admission, mutate_worker_execution,
)
from tests.gateway.test_session_authority_cancel_settlement import _authority
from tests.waiting import async_wait_until


# Real framed process and authenticated reservation, with no model or business code.
CHILD = '''import json, sys, time
from pathlib import Path
from agent.managed_worker import encode_frame, hello
out = sys.stdout.buffer
def send(value):
    out.write(encode_frame(value)); out.flush()
send({'type': 'hello', **hello()})
for line in sys.stdin.buffer:
    value = json.loads(line)
    if 'version' in value:
        if len(sys.argv) > 2 and sys.argv[2] == 'native':
            from agent.managed_worker import bind_worker_policy
            bind_worker_policy(value)
            from agent.runtime_session_store import WorkerRPC
            rpc = WorkerRPC(value['home'])
            scope = value['scope']
            adopted = rpc('worker.adopt', **{k: v for k, v in scope.items() if k != 'epoch'})
            assert adopted['owner_epoch'] == scope['epoch']
        with open(sys.argv[1], 'a') as log: log.write('runner\\n')
        while len(sys.argv) > 2 and sys.argv[2] != 'native' and not Path(sys.argv[2]).exists():
            time.sleep(.01)
        send({'type': 'result', 'result': {'final_response': 'EXACT_ACCEPTED', 'model': 'inert'}})
    elif value == {'type': 'finish'}:
        if len(sys.argv) > 2 and sys.argv[2] == 'native':
            rpc('worker.persist', **scope, sequence=1, operation='execution.finish', payload={})
            rpc.close()
        send({'type': 'finished'})
        break
'''


def legal_seal(db, authority, name, *, text='held', finished=False):
    db.create_session(name, source='test')
    admit_session_input(db, epoch=authority.epoch, principal_id='human', session_id=name,
                        request_id=name, payload={'text': 'held input'})
    row = claim_session_input(db, epoch=authority.epoch, session_id=name)
    execution = 'admission-worker:' + row['admission_id']
    db._execute_write(lambda conn: conn.execute(
        "INSERT INTO worker_executions(execution_id,session_id,kind,owner_epoch,generation,status,adoption_digest) "
        "VALUES(?,?,'compute',?,?,'running','fixture')", (execution, name, authority.epoch, row['generation'])))
    scope = dict(epoch=authority.epoch, profile_id=authority.profile_id, session_id=name,
                 execution_id=execution, generation=row['generation'])
    raw = {'final_response': text}
    accepted, usage = accept_result(raw)
    results.seal_result(authority, row=row, scope=scope, raw_result=raw,
                        captured={'result': {**accepted, 'local_viewer': True}, 'usage': usage})
    if finished:
        mutate_worker_execution(db, epoch=authority.epoch, execution_id=execution, session_id=name,
                                generation=row['generation'], sequence=1, operation='execution.finish', payload={})
    return row, scope


def fill_byte_capacity(db, authority):
    # Derive each real seal's remaining room from its actual encoding, keeping the
    # worker result frame legal. The last seal fills the unmodified 64MiB limit.
    from hermes_state_runtime import _json
    while True:
        used = db._read_one('SELECT COALESCE(SUM(length(CAST(value AS BLOB))),0) FROM state_meta WHERE key LIKE ?',
                            (results.PREFIX + '%',))[0]
        remaining = results.MAX_BYTES - used
        number = db._read_one('SELECT COUNT(*) FROM state_meta WHERE key LIKE ?', (results.PREFIX + '%',))[0]
        row, scope = legal_seal(db, authority, 'bytes-' + str(number), text='')
        value = json.loads(db._read_one('SELECT value FROM state_meta WHERE key=?',
                                       (results.PREFIX + row['admission_id'],))[0])
        overhead = len(_json(value).encode())
        room = MAX_FRAME - len(encode_frame({'type': 'result', 'result': {'final_response': ''}})) - 64
        text = 'x' * min(room, remaining - overhead)
        assert len(encode_frame({'type': 'result', 'result': {'final_response': text}})) < MAX_FRAME
        # Rebuild through the real producer, rather than writing synthetic metadata.
        db._execute_write(lambda conn: conn.execute('DELETE FROM state_meta WHERE key=?',
                                                    (results.PREFIX + row['admission_id'],)))
        raw = {'final_response': text}
        accepted, usage = accept_result(raw)
        results.seal_result(authority, row=row, scope=scope, raw_result=raw,
                            captured={'result': {**accepted, 'local_viewer': True}, 'usage': usage})
        after = db._read_one('SELECT SUM(length(CAST(value AS BLOB))) FROM state_meta WHERE key LIKE ?',
                             (results.PREFIX + '%',))[0]
        if after == results.MAX_BYTES:
            return
        assert after < results.MAX_BYTES


def inert_ingress(tmp_path, monkeypatch, *, gate=None, expansion=False):
    db, authority = _authority(tmp_path, monkeypatch)
    authority.runner._cached_agent_for = lambda route: None
    authority.sessions['s'].source.chat_id = 'c'
    from gateway.session_policy import build_policy
    policy = build_policy({'cwd': str(tmp_path), 'model': 'inert'}, {}, profile_terminal=False)
    monkeypatch.setattr(managed, 'managed_policy', lambda *args: policy)
    marker = tmp_path / 'runner-calls'
    popen = subprocess.Popen
    spawned = []

    class Child(popen):
        def __init__(self, args, **kwargs):
            script = CHILD.replace("'EXACT_ACCEPTED'", "'q' * (3 * 1024 * 1024)") if expansion else CHILD
            command = [sys.executable, '-c', script, str(marker)]
            if gate is not None:
                command.append(str(gate))
            super().__init__(command, **kwargs)
            spawned.append(self)
    monkeypatch.setattr(managed.subprocess, 'Popen', Child)
    return db, authority, SessionRef('owned', 's'), marker, spawned


@pytest.mark.asyncio
@pytest.mark.parametrize('quota', ['rows', 'bytes'])
async def test_exhausted_legal_seals_refuse_at_native_ingress_before_runner(tmp_path, monkeypatch, quota):
    db, authority, ref, marker, spawned = inert_ingress(tmp_path, monkeypatch)
    actor = SessionActor('human', 'owned', frozenset({'session:submit', 'session:control'}), 'cli')
    with db:
        if quota == 'bytes':
            with monkeypatch.context() as fixed:
                fixed.setattr(results, 'time', SimpleNamespace(time=lambda: 1000000.0))
                fill_byte_capacity(db, authority)
        else:
            for number in range(results.MAX_ROWS):
                legal_seal(db, authority, 'held-' + str(number))
        before = [tuple(r) for r in db._read_all('SELECT key,value FROM state_meta WHERE key LIKE ?',
                                                (results.PREFIX + '%',))]
        receipt = await authority.submit(actor, Submission('capacity', ref, {'text': 'must not execute'}, 'queue'))
        await asyncio.wait_for(authority.sessions['s'].task, 30)
        row = get_session_admission(db, admission_id=receipt.admission_id)
        calls = len(marker.read_text().splitlines()) if marker.exists() else 0
        print(json.dumps({'quota': quota, 'runner_calls': calls, 'spawned': len(spawned), 'status': row['status'],
                          'outcome': row['outcome'], 'held': len(before)}))
        assert calls == 0, 'retention exhaustion was discovered only after the runner completed'
        assert not spawned
        assert row['status'] == 'terminal' and row['outcome'] == 'failed'
        saved = admission_result(db, receipt.admission_id)
        assert saved['result']['failure_reason'] == 'managed_result_capacity'
        assert before == [tuple(r) for r in db._read_all('SELECT key,value FROM state_meta WHERE key LIKE ?',
                                                        (results.PREFIX + '%',))]
        # Lost submit ACK: exact retries and reopened stores read the committed refusal.
        replay = await authority.submit(actor, Submission('capacity', ref, {'text': 'must not execute'}, 'queue'))
        assert replay.admission_id == receipt.admission_id and replay.status == 'terminal'
        with SessionDB(db.db_path) as reopened:
            assert admission_result(reopened, receipt.admission_id) == saved
        assert not marker.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['remaining_1_byte', 'single_result_over_64MiB'])
async def test_remaining_byte_capacity_is_decided_before_execution_or_saved_losslessly(tmp_path, monkeypatch, boundary):
    from tests.gateway.test_managed_result_overflow import native_overflow_recovery
    await asyncio.to_thread(native_overflow_recovery, tmp_path, boundary)


@pytest.mark.asyncio
async def test_concurrent_native_turns_cannot_both_take_last_slot(tmp_path, monkeypatch):
    from gateway.session_authority import LiveSession
    slots = 'gateway.managed_result_slot.v1.'
    gate = tmp_path / 'release'
    db, authority, ref, marker, spawned = inert_ingress(tmp_path, monkeypatch, gate=gate)
    actor = SessionActor('human', 'owned', frozenset({'session:submit', 'session:control'}), 'cli')
    with db:
        for number in range(results.MAX_ROWS - 1):
            legal_seal(db, authority, 'held-' + str(number))
        db.create_session('t', source='test')
        authority.sessions['t'] = LiveSession(authority.sessions['s'].source, 'route-t')
        other = SessionRef('owned', 't')
        try:
            receipts = await asyncio.gather(
                authority.submit(actor, Submission('first', ref, {'text': 'first'}, 'queue')),
                authority.submit(actor, Submission('second', other, {'text': 'second'}, 'queue')))
            await async_wait_until(lambda: marker.exists() and (len(marker.read_text().splitlines()) >= 2 or any(
                get_session_admission(db, admission_id=r.admission_id)['status'] == 'terminal' for r in receipts)),
                timeout=30, what='one runner admitted and the competing turn refused')
            assert marker.read_text().splitlines() == ['runner']
            assert len(spawned) == 1
            assert db._read_one('SELECT COUNT(*) FROM state_meta WHERE key LIKE ?', (slots + '%',))[0] == 1
            gate.touch()
            await asyncio.wait_for(asyncio.gather(*(authority.sessions[s].task for s in ['s', 't'])), 30)
            outcomes = sorted(get_session_admission(db, admission_id=r.admission_id)['outcome'] for r in receipts)
            print(json.dumps({'concurrent_runner_calls': 1, 'outcomes': outcomes}))
            assert outcomes == ['completed', 'failed']
            assert db._read_one('SELECT COUNT(*) FROM state_meta WHERE key LIKE ?', (slots + '%',))[0] == 0
        finally:
            gate.touch()
            await asyncio.gather(*(authority.sessions[s].task for s in ['s', 't'] if authority.sessions[s].task),
                                 return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_during_capacity_commit_releases_late_slot_without_runner(tmp_path, monkeypatch):
    import hermes_state_managed_result_capacity as capacity
    db, authority, ref, marker, spawned = inert_ingress(tmp_path, monkeypatch)
    actor = SessionActor('human', 'owned', frozenset({'session:submit', 'session:control'}), 'cli')
    entered, release = threading.Event(), threading.Event()
    reserve = capacity.reserve_capacity
    def held_commit(*args):
        entered.set()
        assert release.wait(10)
        return reserve(*args)
    monkeypatch.setattr(capacity, 'reserve_capacity', held_commit)
    with db:
        receipt = await authority.submit(actor, Submission('cancel-reserve', ref, {'text': 'never execute'}, 'queue'))
        task = authority.sessions['s'].task
        try:
            assert await asyncio.to_thread(entered.wait, 10)
            task.cancel()
        finally:
            release.set()
        await asyncio.wait_for(task, 20)
        await async_wait_until(lambda: not authority._mutation_tasks, timeout=10, what='late reservation release')
        assert not marker.exists() and not spawned
        assert get_session_admission(db, admission_id=receipt.admission_id)['outcome'] == 'interrupted'
        assert db._read_one('SELECT COUNT(*) FROM state_meta WHERE key LIKE ?', (capacity.PREFIX + '%',))[0] == 0


def claimed(db, authority, name):
    db.create_session(name, source='test')
    admit_session_input(db, epoch=authority.epoch, principal_id='human', session_id=name,
                        request_id=name, payload={'text': name})
    return claim_session_input(db, epoch=authority.epoch, session_id=name)


def test_dead_old_owner_slots_release_but_unknown_never_reexecutes(tmp_path):
    from hermes_state_managed_result_capacity import PREFIX as slots, reserve_capacity
    from hermes_state_runtime import recover_session_inputs
    from gateway.managed_result_recovery import recover_completed_managed_results
    program = '''import json, sys
from pathlib import Path
from types import SimpleNamespace
from hermes_state import SessionDB
from hermes_state_runtime import begin_runtime_epoch
from hermes_state_managed_result_capacity import reserve_capacity
from tests.gateway.test_managed_result_capacity import claimed
with SessionDB(Path(sys.argv[1])) as db:
    owner = SimpleNamespace(db=db, epoch=begin_runtime_epoch(db, instance_id='old'), profile_id='owned',
                            _require_admission_open=lambda: None)
    row = claimed(db, owner, 'lost')
    reserve_capacity(owner, row['admission_id'])
    print(json.dumps(row), flush=True)
    sys.stdin.read()
'''
    child = subprocess.Popen([sys.executable, '-c', program, str(tmp_path / 'state.db')],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        row = json.loads(child.stdout.readline())
    finally:
        child.kill()
        child.wait(timeout=10)
        child.stdin.close()
        child.stdout.close()
    with SessionDB(tmp_path / 'state.db') as db:
        old = SimpleNamespace(db=db, epoch=row['owner_epoch'], profile_id='owned', _require_admission_open=lambda: None)
        fresh = SimpleNamespace(db=db, epoch=begin_runtime_epoch(db, instance_id='fresh'), profile_id='owned',
                                _require_admission_open=lambda: None)
        recover_completed_managed_results(db, profile_id='owned', epoch=fresh.epoch)
        recover_session_inputs(db, epoch=fresh.epoch)
        next_row = claimed(db, fresh, 'next')
        reserve_capacity(fresh, next_row['admission_id'])
        assert db._read_one('SELECT key FROM state_meta WHERE key LIKE ?', (slots + '%',))[0] == slots + next_row['admission_id']
        assert get_session_admission(db, admission_id=row['admission_id'])['status'] == 'unknown'
        with pytest.raises(RuntimeStoreError, match='unknown_execution'):
            claim_session_input(db, epoch=fresh.epoch, session_id='lost')
        with pytest.raises(RuntimeStoreError, match='stale_epoch'):
            reserve_capacity(old, row['admission_id'])


def test_old_live_owner_slot_is_not_reclaimed_by_new_epoch(tmp_path, monkeypatch):
    from hermes_state_managed_result_capacity import PREFIX as slots, reserve_capacity
    with SessionDB(tmp_path / 'state.db') as db:
        owner = SimpleNamespace(db=db, epoch=begin_runtime_epoch(db, instance_id='live'), profile_id='owned',
                                _require_admission_open=lambda: None)
        row = claimed(db, owner, 'live')
        reserve_capacity(owner, row['admission_id'])
        owner.epoch = begin_runtime_epoch(db, instance_id='new')
        follower = claimed(db, owner, 'follower')
        monkeypatch.setattr(results, 'MAX_ROWS', 1)
        with pytest.raises(RuntimeStoreError, match='managed_result_capacity'):
            reserve_capacity(owner, follower['admission_id'])
        assert db._read_one('SELECT key FROM state_meta WHERE key LIKE ?', (slots + '%',))[0] == slots + row['admission_id']


def test_legacy_unreserved_worker_counts_until_normal_settlement(tmp_path, monkeypatch):
    from hermes_state_managed_result_capacity import reserve_capacity
    from hermes_state_runtime import settle_session_input
    with SessionDB(tmp_path / 'state.db') as db:
        owner = SimpleNamespace(db=db, epoch=begin_runtime_epoch(db, instance_id='old-version'), profile_id='owned',
                                _require_admission_open=lambda: None)
        row = claimed(db, owner, 'legacy')
        db._execute_write(lambda conn: conn.execute(
            "INSERT INTO worker_executions(execution_id,session_id,kind,owner_epoch,generation,status,adoption_digest) "
            "VALUES(?,'legacy','compute',?,?,'running','legacy')",
            ('admission-worker:' + row['admission_id'], owner.epoch, row['generation'])))
        follower = claimed(db, owner, 'follower')
        monkeypatch.setattr(results, 'MAX_ROWS', 1)
        with pytest.raises(RuntimeStoreError, match='managed_result_capacity'):
            reserve_capacity(owner, follower['admission_id'])
        # Legacy execution.finish without a seal still follows the old public API.
        mutate_worker_execution(db, epoch=owner.epoch, execution_id='admission-worker:' + row['admission_id'],
                                session_id='legacy', generation=row['generation'], sequence=1,
                                operation='execution.finish', payload={})
        settle_session_input(db, epoch=owner.epoch, admission_id=row['admission_id'],
                             generation=row['generation'], outcome='completed')
        reserve_capacity(owner, follower['admission_id'])


def test_wire_limit_does_not_bound_redacted_seal_encoding(tmp_path, monkeypatch):
    from agent import redact
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(redact, '_VAULT_REDACTION_VALUES', {})
    redact.register_vault_redaction_value('q')
    raw = {'final_response': 'q' * (200 * 1024)}
    wire_bytes = len(encode_frame({'type': 'result', 'result': raw}))
    with SessionDB(tmp_path / 'state.db') as db:
        owner = SimpleNamespace(db=db, epoch=begin_runtime_epoch(db, instance_id='owner'), profile_id=str(tmp_path),
                                _require_admission_open=lambda: None)
        row, _ = legal_seal(db, owner, 'expansion', text=raw['final_response'])
        value = db._read_one('SELECT value FROM state_meta WHERE key=?', (results.PREFIX + row['admission_id'],))[0]
        encoded_bytes = len(value.encode())
        assert wire_bytes < MAX_FRAME < encoded_bytes < results.MAX_BYTES
        assert raw['final_response'] not in value
        assert json.loads(value)['body']['captured']['result']['final_response'] == '«redacted-vault-secret»' * (200 * 1024)
        print(json.dumps({'wire_bytes': wire_bytes, 'sealed_bytes': encoded_bytes, 'max_frame': MAX_FRAME}))


def test_inert_finished_seal_survives_real_owner_kill_and_startup_consumes_only(tmp_path):
    from contextlib import closing
    import sqlite3
    from tests.gateway.fixtures.local_recovery_probe import child_env, daemon, rpc, websocket
    root = Path(__file__).resolve().parents[2]
    home, user = tmp_path / 'state', tmp_path / 'user'
    home.mkdir()
    user.mkdir()
    marker, sealed = tmp_path / 'runner-calls', tmp_path / 'sealed-finished'
    (home / 'config.yaml').write_text(json.dumps({
        'gateway': {'multiplex_profiles': False, 'managed_workers': True},
        'model': {'provider': 'custom', 'default': 'inert', 'base_url': 'http://127.0.0.1:1/v1'},
        'auxiliary': {'title_generation': {'enabled': False}}, 'platform_toolsets': {'cli': []}}))
    env = child_env()
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home), PYTHONPATH=str(root),
               CAPACITY_RUNNER_MARKER=str(marker), CAPACITY_FINISH_MARKER=str(sealed))

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
            reply = await rpc(ws, 'prompt.submit', session_id=sid, input_id='lost-ack', text='inert')
            assert 'result' in reply, reply
            await async_wait_until(sealed.exists, timeout=45, what='durable seal and finish before settlement')
            [(admission, status)] = query('SELECT admission_id,status FROM session_admissions WHERE request_id=?', ('lost-ack',))
            assert status == 'started'
            assert query('SELECT status FROM worker_executions WHERE execution_id=?', ('admission-worker:' + admission,)) == [('terminal',)]
            owner.kill()
            await asyncio.to_thread(owner.wait, 10)
            return sid, admission

    async def receipt(desc, sid, admission):
        async with websocket(home, desc) as ws:
            reply = await rpc(ws, 'prompt.receipt', session_id=sid, admission_id=admission, include_result=True)
            assert reply.get('result', {}).get('status') == 'terminal', reply
            assert reply['result']['outcome'] == 'completed'
            assert reply['result']['result']['final_response'] == 'EXACT_ACCEPTED'
            resumed = await rpc(ws, 'session.resume', session_id=sid)
            assert 'result' in resumed, resumed
            replay = await rpc(ws, 'prompt.submit', session_id=sid, input_id='lost-ack', text='inert')
            assert replay.get('result', {}).get('admission_id') == admission and replay['result']['status'] == 'terminal', replay
            return reply['result']['result']

    with daemon(root, home, env, barrier=True, fixture=str(Path(__file__).resolve())) as (owner, desc):
        sid, admission = asyncio.run(first(desc, owner))
        epoch = desc['authority_epoch']
    with daemon(root, home, env, barrier=False) as (_, desc):
        assert desc['authority_epoch'] > epoch
        saved = asyncio.run(receipt(desc, sid, admission))
    with daemon(root, home, env, barrier=False) as (_, desc):
        assert asyncio.run(receipt(desc, sid, admission)) == saved
    assert marker.read_text().splitlines() == ['runner']
    assert query('SELECT COUNT(*) FROM state_meta WHERE key LIKE ?', (results.PREFIX + '%',)) == [(0,)]
    print(json.dumps({'real_owner_kill': True, 'restarts': 2, 'inert_runner_calls': 1, 'model_calls': 0}))


if __name__ == '__main__':
    import runpy
    original_popen = subprocess.Popen
    class InertChild(original_popen):
        def __init__(self, args, **kwargs):
            if args[-2:] == ['-m', 'agent.managed_worker']:
                args = [sys.executable, '-c', CHILD, os.environ['CAPACITY_RUNNER_MARKER'], 'native']
            super().__init__(args, **kwargs)
    managed.subprocess.Popen = InertChild
    async def after_finish(authority, session_id, accepted):
        Path(os.environ['CAPACITY_FINISH_MARKER']).touch()
        await asyncio.Future()
    managed._finalize_managed_refusal = after_finish
    runpy.run_module('gateway.run', run_name='__main__')
