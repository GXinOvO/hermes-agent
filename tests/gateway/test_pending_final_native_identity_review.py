"""Independent native HUMAN writer/reader/ACK probes on frozen product bytes."""
import hashlib
import json
from copy import deepcopy
import sqlite3

import pytest
import pytest_asyncio

from gateway import pending_final as pf
from gateway.platforms.base import SendResult
from gateway.session_results import finish_result
from hermes_state_terminal import RESULT_PREFIX, load_result


@pytest_asyncio.fixture
async def native(tmp_path, monkeypatch):
    from gateway.config import GatewayConfig, Platform
    from gateway.platforms.event import MessageEvent
    from gateway.run import GatewayRunner
    from gateway.session_authority import SessionAuthority, initialize_session_authority
    from gateway.session_ingress_context import native_callback
    from hermes_state_runtime import claim_session_input
    from tests.gateway.test_shared_busy_receive_loop import Adapter
    import hermes_state
    monkeypatch.setenv('HOME', str(tmp_path / 'user'))
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('TMPDIR', str(tmp_path))
    monkeypatch.setenv('TELEGRAM_ALLOWED_USERS', 'review-native-user')
    monkeypatch.setattr(hermes_state, 'DEFAULT_DB_PATH', tmp_path / 'state.db')
    monkeypatch.setattr(SessionAuthority, '_schedule', lambda self, ref: None)
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / 'sessions'))
    adapter = Adapter()
    runner.adapters = {Platform.TELEGRAM: adapter}
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='sol-core-native-02')
    runner._wire_adapter_handlers(adapter)
    canonical = {'message': {'messageId': 'sol-native-input', 'parts': [{'text': 'native original input'}],
        'metadata': {'review': 'original'}, 'configuration': {'mode': 'original'}}}
    fingerprint = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    event = MessageEvent(text='native original input', message_id='sol-native-input',
        source=adapter.build_source(chat_id='sol-native-chat', chat_type='dm', user_id='review-native-user'))
    event._ingress_sha256 = fingerprint
    with native_callback(runner, event, tmp_path):
        admitted = await authority.admit_native(event)
    row = claim_session_input(authority.db, epoch=authority.epoch, session_id=admitted.ref.session_id)
    assert row['principal_id'].startswith('messaging:')
    assert row['payload']['native_text_v1']['ingress_sha256'] == fingerprint
    route = row['payload']['native_text_v1']['route']
    descriptor = await pf.prepare(authority, row, event, route, 'sol sealed answer')
    assert descriptor['binding']['policy'] == 'human_reply_v1'
    try:
        yield authority.db, authority.epoch, row, descriptor, tmp_path, fingerprint
    finally:
        authority.db.close()


def settle(native):
    db, epoch, row, descriptor, _home, _fingerprint = native
    return finish_result(db, epoch=epoch, row=row, response='sol sealed answer', outcome='completed',
        result={'result': {'final_response': 'sol sealed answer', 'messages': [
            {'role': 'user', 'content': row['payload']['text']}, {'role': 'assistant', 'content': 'sol sealed answer'}],
            '_pending_final_v1': descriptor}, 'usage': {},
            '_pending_final_identity_v1': {'version': 999, 'binding': {'untrusted': True}}})


def result_raw(db, row):
    with db._read_ctx() as conn:
        return conn.execute('SELECT value FROM state_meta WHERE key=?', (RESULT_PREFIX + row['admission_id'],)).fetchone()[0]


def fault(native, mode):
    db, _epoch, row, _descriptor, _home, _fingerprint = native
    def write(conn):
        value = pf._read(conn, row['admission_id'])
        payload = deepcopy(row['payload'])
        generation, principal, request, target, owner = row['generation'], row['principal_id'], row['request_id'], row['target_session_id'], row['owner_epoch']
        if mode in {'generation','generation_no_marker','generation_no_witness','generation_stripped_both'}:
            generation += 1
            conn.execute('UPDATE sessions SET runtime_generation=? WHERE id=?', (generation, target))
        if mode == 'route':
            payload['native_text_v1']['route'] += ':drift'
        if mode == 'request':
            request += ':drift'
        if mode == 'principal':
            principal += ':drift'
        if mode == 'source':
            payload['native_text_v1']['source']['chat_id'] += ':drift'
        if mode == 'fullinput':
            payload['native_text_v1']['event']['review_extra'] = {'parts': [1], 'metadata': {'changed': True}}
        if mode == 'ingress':
            payload['native_text_v1']['ingress_sha256'] = 'd' * 64
        if mode == 'owner_epoch':
            owner += 1
        if mode == 'session':
            target += ':drift'
        conn.execute('UPDATE session_admissions SET generation=?,principal_id=?,request_id=?,target_session_id=?,owner_epoch=?,payload_json=? WHERE admission_id=?',
            (generation, principal, request, target, owner, json.dumps(payload), row['admission_id']))
        raw = dict(conn.execute('SELECT * FROM session_admissions WHERE admission_id=?', (row['admission_id'],)).fetchone())
        value['binding'] = pf._binding(raw, db.db_path)
        if mode in {'profile'}:
            value['binding']['profile_home'] += '/foreign'
        if mode in {'generation_no_marker','generation_stripped_both','marker_only'}:
            value.pop('identity_witness_version')
        if mode in {'generation_no_witness','generation_stripped_both'}:
            saved = load_result(conn.execute('SELECT value FROM state_meta WHERE key=?', (RESULT_PREFIX + row['admission_id'],)).fetchone()[0])
            saved.pop('_pending_final_identity_v1')
            conn.execute('UPDATE state_meta SET value=? WHERE key=?', (json.dumps(saved), RESULT_PREFIX + row['admission_id']))
        pf._put(conn, row['admission_id'], value)
    db._execute_write(write)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['none','generation','route','request','principal','source','fullinput','ingress','owner_epoch','session','profile','marker_only','generation_no_marker','generation_no_witness'])
async def test_native_original_identity_before_any_prepare_pin_or_claim(native, mode):
    db, _epoch, row, _descriptor, home, fingerprint = native
    settle(native)
    original = result_raw(db, row)
    if mode == 'session':
        db.create_session(row['target_session_id'] + ':drift', source='telegram')
    if mode != 'none':
        fault(native, mode)
    value = pf.pending(db)[0]
    before_read = result_raw(db, row)
    with db._read_ctx() as conn:
        changes = conn.total_changes
        try:
            witness = pf.read_identity_witness(conn, home, value['binding'])
            error = None
        except ValueError as exc:
            witness, error = None, str(exc)
        assert conn.total_changes == changes
    result, won, reason = pf.claim(db, row['admission_id'], 'text', 'sol sealed answer')
    print('SOL_NATIVE_IDENTITY', json.dumps({'mode': mode, 'won': won, 'reason': reason,
        'attempts': result['parts']['text']['attempts'], 'reader_error': error,
        'witness_present': witness is not None, 'policy': result['binding']['policy'],
        'generation': result['binding']['generation'], 'original_generation': row['generation'],
        'original_ingress_sha256': fingerprint, 'result_unchanged': result_raw(db,row)==before_read}))
    assert result_raw(db, row) == before_read
    if mode not in {'generation_no_witness','generation_stripped_both'}:
        assert original == before_read
    assert won is (mode == 'none'), 'new native witness must not downgrade after marker/witness deletion'
    assert result['parts']['text']['attempts'] == int(mode == 'none')






@pytest.mark.asyncio
@pytest.mark.parametrize('bad_clock', [True, -1, float('nan'), float('inf'), 10**400])
async def test_native_invalid_ack_clock_rolls_back(native, monkeypatch, bad_clock):
    db, _epoch, row, _descriptor, _home, _fingerprint = native
    settle(native)
    assert pf.claim(db,row['admission_id'],'text','sol sealed answer')[1]
    before = pf.pending(db)[0]
    # First clock read is the existing claim-age guard; inject only the ACK stamp.
    timestamps = iter([pf.time.time(), bad_clock])
    with monkeypatch.context() as clock:
        clock.setattr(pf.time,'time',lambda:next(timestamps))
        with pytest.raises(ValueError, match='ACK time'):
            pf.finish(db,row['admission_id'],'text',SendResult(success=True))
    current = pf.pending(db)[0]
    assert current == before and 'delivered_at' not in current['parts']['text']
    print('SOL_NATIVE_INVALID_ACK_CLOCK',json.dumps({'clock_type':type(bad_clock).__name__, 'rolled_back':True}))


@pytest.mark.asyncio
@pytest.mark.parametrize('raw', ['null', '[]', '7', '{"native_text_v1":7}'])
async def test_native_bad_admission_is_value_error_and_preserves_raw(native, raw):
    db, _epoch, row, _descriptor, home, _fingerprint = native
    settle(native)
    db._execute_write(lambda conn: conn.execute('UPDATE session_admissions SET payload_json=? WHERE admission_id=?',(raw,row['admission_id'])))
    value = pf.pending(db)[0]
    with db._read_ctx() as conn:
        before = conn.total_changes
        try:
            pf.read_identity_witness(conn,home,value['binding'])
        except Exception as exc:
            failure = exc
        else:
            failure = None
        assert conn.total_changes == before
        assert conn.execute('SELECT payload_json FROM session_admissions WHERE admission_id=?',(row['admission_id'],)).fetchone()[0] == raw
    try:
        _value,won,reason = pf.claim(db,row['admission_id'],'text','sol sealed answer')
        claim_error = None
    except Exception as exc:
        won,reason,claim_error = False,None,type(exc).__name__
    print('SOL_NATIVE_CORRUPT_ADMISSION',json.dumps({'raw':raw,'reader_error':type(failure).__name__, 'claim_error':claim_error, 'won':won,'reason':reason,'attempts':pf.pending(db)[0]['parts']['text']['attempts']}))
    assert isinstance(failure,ValueError), 'read API must normalize corrupt admission into ValueError'
    assert claim_error is None and not won and reason == 'invalidated'


@pytest.mark.asyncio
async def test_native_terminal_atomic_abort_retry_and_idempotency(native):
    db, _epoch, row, _descriptor, home, fingerprint = native
    db._execute_write(lambda conn: conn.execute("CREATE TRIGGER sol_abort_result BEFORE INSERT ON state_meta WHEN NEW.key LIKE 'gateway.admission.result.v1.%' BEGIN SELECT RAISE(ABORT,'sol result insert fault'); END"))
    with pytest.raises(sqlite3.IntegrityError, match='sol result insert fault'):
        settle(native)
    assert pf.pending(db) == []
    with db._read_ctx() as conn:
        assert conn.execute('SELECT status FROM session_admissions WHERE admission_id=?',(row['admission_id'],)).fetchone()[0] == 'started'
        assert conn.execute('SELECT value FROM state_meta WHERE key=?',(RESULT_PREFIX+row['admission_id'],)).fetchone() is None
    db._execute_write(lambda conn: conn.execute('DROP TRIGGER sol_abort_result'))
    settle(native)
    saved, final = result_raw(db, row), pf.pending(db)[0]
    with db._read_ctx() as conn:
        witness = pf.read_identity_witness(conn, home, final['binding'])
        assert witness['ingress_sha256'] == fingerprint and witness['version'] == 1
    settle(native)
    assert result_raw(db,row) == saved and pf.pending(db)[0] == final
    print('SOL_NATIVE_TRANSACTION', json.dumps({'abort_after_seal_insert':'rolled_back', 'retry':'committed', 'duplicate_finish':'byte_identical', 'untrusted_top_level_witness':'replaced_by_writer'}))


@pytest.mark.asyncio
async def test_native_sqlite_busy_retry_reexecutes_real_terminal_writer(native, monkeypatch):
    db, _epoch, _row, _descriptor, home, fingerprint = native
    original = pf.persist
    calls = []
    def once_busy(conn, current, prepared, *, db_path):
        witness = original(conn, current, prepared, db_path=db_path)
        calls.append(witness)
        if len(calls) == 1:
            raise sqlite3.OperationalError('database is locked')
        return witness
    monkeypatch.setattr(pf,'persist',once_busy)
    settle(native)
    assert len(calls) == 2 and calls[0] == calls[1]
    with db._read_ctx() as conn:
        assert pf.read_identity_witness(conn,home,pf.pending(db)[0]['binding'])['ingress_sha256'] == fingerprint
    print('SOL_NATIVE_RETRY', json.dumps({'terminal_callback_calls':len(calls), 'duplicate_seal':False}))


@pytest.mark.asyncio
@pytest.mark.parametrize('ack', [True, False])
async def test_native_ack_commit_and_unknown_are_distinct(native, ack):
    db, _epoch, row, _descriptor, _home, _fingerprint = native
    settle(native)
    assert pf.claim(db,row['admission_id'],'text','sol sealed answer')[1]
    saved = result_raw(db,row)
    assert pf.finish(db,row['admission_id'],'text',SendResult(success=ack))
    part = pf.pending(db)[0]['parts']['text']
    assert (part['state'], 'delivered_at' in part) == (('delivered',True) if ack else ('unknown',False))
    assert result_raw(db,row) == saved
    print('SOL_NATIVE_ACK', json.dumps({'ack':ack,'part':part}))


@pytest.mark.asyncio
async def test_native_ack_result_write_failure_does_not_consume(native):
    db, _epoch, row, _descriptor, _home, _fingerprint = native
    settle(native)
    assert pf.claim(db,row['admission_id'],'text','sol sealed answer')[1]
    before = pf.pending(db)[0]
    db._execute_write(lambda conn: conn.execute("CREATE TRIGGER sol_abort_ack BEFORE UPDATE ON state_meta WHEN NEW.key LIKE 'gateway.pending_final.v1:%' BEGIN SELECT RAISE(ABORT,'sol ACK write fault'); END"))
    with pytest.raises(sqlite3.IntegrityError, match='sol ACK write fault'):
        pf.finish(db,row['admission_id'],'text',SendResult(success=True))
    assert pf.pending(db)[0] == before
    assert 'delivered_at' not in before['parts']['text']
    db._execute_write(lambda conn: conn.execute('DROP TRIGGER sol_abort_ack'))
    assert pf.finish(db,row['admission_id'],'text',SendResult(success=True))
    print('SOL_NATIVE_ACK_ROLLBACK',json.dumps({'failed_commit':'attempting_without_timestamp','retry':'delivered'}))


@pytest.mark.asyncio
async def test_native_result_compaction_retirement_and_pruning_fail_closed(native):
    from hermes_state_mutation_retirement import _compact_result, retire_sessions, prune_retired_tombstones
    db, _epoch, row, _descriptor, home, _fingerprint = native
    settle(native)
    value = pf.pending(db)[0]
    original = load_result(result_raw(db,row))['_pending_final_identity_v1']
    db._execute_write(lambda conn: _compact_result(conn,row['admission_id']))
    with db._read_ctx() as conn:
        assert pf.read_identity_witness(conn,home,value['binding']) == original
    db._execute_write(lambda conn: retire_sessions(conn,[row['target_session_id']]))
    assert load_result(result_raw(db,row))['_pending_final_identity_v1'] == original
    assert pf.claim(db,row['admission_id'],'text','sol sealed answer')[1:] == (False,'invalidated')
    db._execute_write(lambda conn: prune_retired_tombstones(conn,older_than=-1))
    with db._read_ctx() as conn:
        assert pf.read_identity_witness(conn,home,value['binding']) is None
    current,won,reason=pf.claim(db,row['admission_id'],'text','sol sealed answer')
    assert not won and reason=='invalidated' and current['parts']['text']['attempts']==0
    assert current['parts']['text']['state']=='pending' and 'delivered_at' not in current['parts']['text']
    print('SOL_NATIVE_LIFECYCLE',json.dumps({'compaction':'witness_preserved','retirement':'no_claim','pruning':'missing_witness_no_reconstruction','part_state':current['parts']['text']['state']}))
