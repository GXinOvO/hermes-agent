from copy import deepcopy
import json


import pytest

from gateway import pending_final as pf
from gateway.session_results import finish_result
from hermes_state_runtime import RuntimeStoreError
from hermes_state_terminal import RESULT_PREFIX, load_result
from tests.gateway import test_pending_final as fixtures


def settle(db, epoch, row, prepared):
    return finish_result(db, epoch=epoch, row=row, response='sealed final', outcome='completed',
        result={'result': {'final_response': 'sealed final', 'messages': [], '_pending_final_v1': prepared}, 'usage': {}})


def ingress(db, row):
    payload = deepcopy(row['payload'])
    payload['native_text_v1']['ingress_sha256'] = 'a' * 64
    def write(conn):
        conn.execute('UPDATE session_admissions SET payload_json=? WHERE admission_id=?',
                     (json.dumps(payload), row['admission_id']))
    db._execute_write(write)
    row['payload'] = payload


@pytest.mark.asyncio
@pytest.mark.parametrize('drift', ['none', 'generation', 'route', 'principal', 'source', 'request', 'ingress', 'payload'])
async def test_original_final_identity_refuses_coordinated_current_binding_drift(tmp_path, drift):
    db, epoch, row, event, _, authority = await fixtures._sealed(tmp_path, human=True)
    try:
        ingress(db, row)
        prepared = await pf.prepare(authority, row, event, row['payload']['native_text_v1']['route'], 'sealed final')
        settle(db, epoch, row, prepared)
        original = pf.pending(db)[0]
        with db._read_ctx() as conn:
            result_raw = conn.execute('SELECT value FROM state_meta WHERE key=?', (RESULT_PREFIX + row['admission_id'],)).fetchone()[0]
        current = deepcopy(original)
        def mutate(conn):
            payload = deepcopy(row['payload'])
            values = {'generation': row['generation'], 'principal_id': row['principal_id'], 'request_id': row['request_id']}
            if drift == 'generation':
                values['generation'] += 1
                conn.execute('UPDATE sessions SET runtime_generation=? WHERE id=?', (values['generation'], row['target_session_id']))
            if drift == 'route':
                payload['native_text_v1']['route'] += ':changed'
            if drift == 'principal':
                values['principal_id'] = 'messaging:changed'
            if drift == 'request':
                values['request_id'] = 'changed-request'
            if drift == 'source':
                payload['native_text_v1']['source']['chat_id'] = 'changed-chat'
            if drift == 'ingress':
                payload['native_text_v1']['ingress_sha256'] = 'b' * 64
            if drift == 'payload':
                payload['text'] = 'changed original input'
            conn.execute('UPDATE session_admissions SET generation=?,principal_id=?,request_id=?,payload_json=? WHERE admission_id=?',
                (values['generation'], values['principal_id'], values['request_id'], json.dumps(payload), row['admission_id']))
            raw = dict(conn.execute('SELECT * FROM session_admissions WHERE admission_id=?', (row['admission_id'],)).fetchone())
            current['binding'] = pf._binding(raw, db.db_path)
            pf._put(conn, row['admission_id'], current)
        if drift != 'none':
            db._execute_write(mutate)
        with db._read_ctx() as conn:
            if drift == 'none':
                witness = pf.read_identity_witness(conn, tmp_path, current['binding'])
                assert witness['binding'] == original['binding']
                assert witness['ingress_sha256'] == 'a' * 64
                assert witness['version'] == 1
            else:
                with pytest.raises(ValueError, match='identity witness'):
                    pf.read_identity_witness(conn, tmp_path, current['binding'])
            assert conn.execute('SELECT value FROM state_meta WHERE key=?', (RESULT_PREFIX + row['admission_id'],)).fetchone()[0] == result_raw
        value, won, state = pf.claim(db, row['admission_id'], 'text', 'sealed final')
        assert won is (drift == 'none'), (drift, state)
        assert value['parts']['text']['attempts'] == int(drift == 'none')
        assert value['binding']['policy'] == 'human_reply_v1'
    finally:
        db.close()


@pytest.mark.asyncio
async def test_identity_witness_rolls_back_with_failed_settlement(tmp_path):
    db, epoch, row, event, _, authority = await fixtures._sealed(tmp_path)
    try:
        ingress(db, row)
        prepared = await pf.prepare(authority, row, event, row['payload']['native_text_v1']['route'], 'sealed final')
        def broken(conn, *args, **kwargs):
            pf.persist(conn, row, prepared, db_path=db.db_path)
            raise RuntimeError('abort witness commit')
        from hermes_state_runtime import settle_session_input
        with pytest.raises(RuntimeError, match='abort witness'):
            settle_session_input(db, epoch=epoch, admission_id=row['admission_id'], generation=row['generation'],
                outcome='completed', _terminal_write=broken)
        assert pf.pending(db) == []
        with db._read_ctx() as conn:
            assert pf.read_identity_witness(conn, tmp_path, prepared['binding']) is None
            assert conn.execute('SELECT value FROM state_meta WHERE key=?', (RESULT_PREFIX + row['admission_id'],)).fetchone() is None
        settle(db, epoch, row, prepared)
        with db._read_ctx() as conn:
            assert pf.read_identity_witness(conn, tmp_path, prepared['binding'])['ingress_sha256'] == 'a' * 64
    finally:
        db.close()


@pytest.mark.asyncio
async def test_current_row_drift_before_terminal_write_rolls_back(tmp_path):
    db, epoch, row, event, _, authority = await fixtures._sealed(tmp_path)
    try:
        ingress(db, row)
        prepared = await pf.prepare(authority, row, event, row['payload']['native_text_v1']['route'], 'sealed final')
        payload = deepcopy(row['payload'])
        payload['native_text_v1']['ingress_sha256'] = 'b' * 64
        db._execute_write(lambda conn: conn.execute('UPDATE session_admissions SET payload_json=? WHERE admission_id=?',
                                                   (json.dumps(payload), row['admission_id'])))
        with pytest.raises((RuntimeError, RuntimeStoreError), match='binding|identity'):
            settle(db, epoch, row, prepared)
        assert pf.pending(db) == []
        with db._read_ctx() as conn:
            assert conn.execute('SELECT status FROM session_admissions WHERE admission_id=?', (row['admission_id'],)).fetchone()[0] == 'started'
            assert pf.read_identity_witness(conn, tmp_path, prepared['binding']) is None
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('missing', ['witness', 'ingress'])
async def test_legacy_identity_read_never_reconstructs_original_witness(tmp_path, missing):
    db, epoch, row, event, _, authority = await fixtures._sealed(tmp_path, human=True)
    try:
        if missing == 'witness':
            ingress(db, row)
        prepared = await pf.prepare(authority, row, event, row['payload']['native_text_v1']['route'], 'sealed final')
        settle(db, epoch, row, prepared)
        if missing == 'witness':
            def strip(conn):
                raw = conn.execute('SELECT value FROM state_meta WHERE key=?', (RESULT_PREFIX + row['admission_id'],)).fetchone()[0]
                saved = load_result(raw)
                saved.pop('_pending_final_identity_v1')
                conn.execute('UPDATE state_meta SET value=? WHERE key=?', (json.dumps(saved), RESULT_PREFIX + row['admission_id']))
            db._execute_write(strip)
        with db._read_ctx() as conn:
            before = conn.total_changes
            witness = pf.read_identity_witness(conn, tmp_path, prepared['binding'])
            assert conn.total_changes == before
            if missing == 'witness':
                assert witness is None
            else:
                assert witness['ingress_sha256'] is None
        if missing == 'witness':
            assert pf.claim(db, row['admission_id'], 'text', 'sealed final')[1:] == (False, 'invalidated')
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('drift', [False, True])
async def test_real_native_human_ingress_witness_rejects_first_prepare_generation_drift(tmp_path, monkeypatch, drift):
    from gateway.config import GatewayConfig, Platform
    from gateway.platforms.event import MessageEvent
    from gateway.run import GatewayRunner
    from gateway.session_authority import SessionAuthority, initialize_session_authority
    from gateway.session_ingress_context import native_callback
    from hermes_state_runtime import claim_session_input
    from tests.gateway.test_shared_busy_receive_loop import Adapter
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    import hermes_state
    monkeypatch.setattr(hermes_state, 'DEFAULT_DB_PATH', tmp_path / 'state.db')
    monkeypatch.setenv('TELEGRAM_ALLOWED_USERS', 'fixture-user')
    monkeypatch.setattr(SessionAuthority, '_schedule', lambda self, ref: None)
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / 'sessions'))
    adapter = Adapter()
    runner.adapters = {Platform.TELEGRAM: adapter}
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='witness-native')
    runner._wire_adapter_handlers(adapter)
    event = MessageEvent(text='native original input', message_id='native-witness-request',
        source=adapter.build_source(chat_id='witness-chat', chat_type='dm', user_id='fixture-user'))
    event._ingress_sha256 = 'c' * 64
    try:
        with native_callback(runner, event, tmp_path):
            admitted = await authority.admit_native(event)
        row = claim_session_input(authority.db, epoch=authority.epoch, session_id=admitted.ref.session_id)
        assert row['principal_id'].startswith('messaging:')
        route = row['payload']['native_text_v1']['route']
        assert row['payload']['native_text_v1']['ingress_sha256'] == 'c' * 64
        prepared = await pf.prepare(authority, row, event, route, 'sealed final')
        settle(authority.db, authority.epoch, row, prepared)
        value = pf.pending(authority.db)[0]
        assert value['binding']['policy'] == 'human_reply_v1'
        if drift:
            def mutate(conn):
                generation = row['generation'] + 1
                conn.execute('UPDATE sessions SET runtime_generation=? WHERE id=?', (generation, row['target_session_id']))
                conn.execute('UPDATE session_admissions SET generation=? WHERE admission_id=?', (generation, row['admission_id']))
                value['binding']['generation'] = generation
                pf._put(conn, row['admission_id'], value)
            authority.db._execute_write(mutate)
        with authority.db._read_ctx() as conn:
            if drift:
                with pytest.raises(ValueError, match='identity witness'):
                    pf.read_identity_witness(conn, tmp_path, value['binding'])
            else:
                assert pf.read_identity_witness(conn, tmp_path, value['binding'])['ingress_sha256'] == 'c' * 64
        result, won, _ = pf.claim(authority.db, row['admission_id'], 'text', 'sealed final')
        assert won is (not drift)
        assert result['parts']['text']['attempts'] == int(not drift)
    finally:
        authority.db.close()
