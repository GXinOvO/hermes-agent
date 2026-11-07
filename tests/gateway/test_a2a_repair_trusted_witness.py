import json

import pytest

from gateway import pending_final as pf
from gateway.config import PlatformConfig
from hermes_state import SessionDB
from plugins.platforms.a2a import durable_tasks as dt
from plugins.platforms.a2a.adapter import A2AAdapter
from tests.gateway import test_a2a_durable_public as public


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path / 'user'))


def test_legacy_without_original_ingress_is_preserved_before_claim(tmp_path):
    identity = public.seed(tmp_path, 'seal-legacy')
    db = SessionDB(tmp_path / 'state.db')
    try:
        with db._read_ctx() as conn:
            raw = list(conn.execute('SELECT key,value FROM state_meta ORDER BY key'))
        result = public.recover(tmp_path, 'recover')
        assert result['delivered'] == 0
        assert (result['part']['state'], result['part']['attempts']) == ('pending', 0)
        with db._read_ctx() as conn:
            assert list(conn.execute('SELECT key,value FROM state_meta ORDER BY key')) == raw
        assert (tmp_path / 'executions').read_text() == '1'
        assert 'sealed native answer' not in result['public']['GetTask']
        assert identity['task'] in result['public']['GetTask']
    finally:
        db.close()


def test_prepare_uses_trusted_result_witness_without_minting_plugin_pin(tmp_path):
    identity = public.seed(tmp_path)
    db = SessionDB(tmp_path / 'state.db')
    try:
        value = pf.pending(db)[0]
        adapter = A2AAdapter(PlatformConfig(enabled=True, extra={'port': 0}))
        with db._read_ctx() as conn:
            raw = list(conn.execute('SELECT key,value FROM state_meta ORDER BY key'))
            assert pf.read_identity_witness(conn, tmp_path, value['binding']) is not None
        dt.prepare(tmp_path, value['binding']['source']['chat_id'], value['parts']['text']['content'],
                   identity['task'], value['metadata'], adapter._agents)
        with db._read_ctx() as conn:
            assert list(conn.execute('SELECT key,value FROM state_meta ORDER BY key')) == raw
        assert public.recover(tmp_path, 'recover')['delivered'] == 1
    finally:
        db.close()


@pytest.mark.parametrize('mutation', ['generation', 'route', 'missing_witness', 'missing_ingress', 'complete_anchor'])
def test_original_witness_rejects_complete_drift_before_claim(tmp_path, mutation):
    from plugins.platforms.a2a import task_bindings as tb
    from gateway.pending_final_identity import FIELD
    from hermes_state_terminal import RESULT_PREFIX, load_result
    public.seed(tmp_path)
    db = SessionDB(tmp_path / 'state.db')
    try:
        value = pf.pending(db)[0]
        admission = value['binding']['admission_id']
        def damage(conn):
            row = conn.execute('SELECT * FROM session_admissions WHERE admission_id=?', (admission,)).fetchone()
            payload = json.loads(row['payload_json'])
            if mutation == 'generation':
                generation = row['generation'] + 1
                conn.execute('UPDATE sessions SET runtime_generation=? WHERE id=?', (generation, row['target_session_id']))
                conn.execute('UPDATE session_admissions SET generation=? WHERE admission_id=?', (generation, admission))
                value['binding']['generation'] = generation
            if mutation == 'route':
                route = 'agent:other:a2a:dm:real-context'
                payload['native_text_v1']['route'] = route
                conn.execute('UPDATE session_admissions SET payload_json=?,principal_id=? WHERE admission_id=?',
                             (tb.encode(payload), 'automation:' + route, admission))
                current = dict(conn.execute('SELECT * FROM session_admissions WHERE admission_id=?', (admission,)).fetchone())
                value['binding'] = pf._binding(current, db.db_path)
                assert value['binding'] is not None
            if mutation == 'missing_witness':
                saved = load_result(conn.execute('SELECT value FROM state_meta WHERE key=?', (RESULT_PREFIX + admission,)).fetchone()[0])
                saved.pop(FIELD)
                conn.execute('UPDATE state_meta SET value=? WHERE key=?', (tb.encode(saved), RESULT_PREFIX + admission))
                value.pop('identity_witness_version')
            if mutation == 'missing_ingress':
                payload['native_text_v1'].pop('ingress_sha256')
                conn.execute('UPDATE session_admissions SET payload_json=? WHERE admission_id=?', (tb.encode(payload), admission))
                current = dict(conn.execute('SELECT * FROM session_admissions WHERE admission_id=?', (admission,)).fetchone())
                value['binding'] = pf._binding(current, db.db_path)
            if mutation == 'complete_anchor':
                key = tb.key(tb.INGRESS_PREFIX, value['reply_to'])
                anchor = json.loads(conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()[0])
                anchor['input']['metadata'] = {'changed': 'canonical input'}
                anchor['input_hash'] = tb.digest(anchor['input'])
                conn.execute('UPDATE state_meta SET value=? WHERE key=?', (tb.encode(anchor), key))
                key = dt._key(value['reply_to'])
                task = json.loads(conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()[0])
                task['input_hash'] = anchor['input_hash']
                conn.execute('UPDATE state_meta SET value=? WHERE key=?', (tb.encode(task), key))
            conn.execute('UPDATE state_meta SET value=? WHERE key=?', (tb.encode(value), pf.PREFIX + admission))
        db._execute_write(damage)
        with db._read_ctx() as conn:
            before = list(conn.execute('SELECT key,value FROM state_meta ORDER BY key'))
        result = public.recover(tmp_path, 'recover')
        assert result['delivered'] == 0
        assert (result['part']['state'], result['part']['attempts']) == ('pending', 0)
        with db._read_ctx() as conn:
            assert list(conn.execute('SELECT key,value FROM state_meta ORDER BY key')) == before
        assert (tmp_path / 'executions').read_text() == '1'
    finally:
        db.close()
