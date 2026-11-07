from copy import deepcopy
from dataclasses import replace
import hashlib
import json

import pytest

from gateway import pending_final as pf
from gateway.config import PlatformConfig
from gateway.strict_transport import strict_transport_scope
from hermes_state import SessionDB
from plugins.platforms.a2a import security
from plugins.platforms.a2a.adapter import A2AAdapter
from tests.gateway import test_a2a_durable_public as public


@pytest.mark.asyncio
async def test_strict_publication_returns_exact_original_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path / 'user'))
    identity = public.seed(tmp_path)
    db = SessionDB(tmp_path / 'state.db')
    try:
        value = pf.pending(db)[0]
        admission = value['binding']['admission_id']
        text = value['parts']['text']['content']
        _value, won, reason = pf.claim(db, admission, 'text', text, recovering=True)
        assert won and reason == 'attempting'
        adapter = A2AAdapter(PlatformConfig(enabled=True, extra={'port': 0}))
        with strict_transport_scope():
            result = await adapter.send(value['binding']['source']['chat_id'], text,
                                        reply_to=identity['task'], metadata=value['metadata'])
        assert result.success, result.error
        assert isinstance(result.raw_response, dict)
        capability = result.raw_response['publication_receipt_v1']
        expected = {'version': 1, 'platform': 'a2a', 'admission_id': admission,
                    'task_id': identity['task'], 'context_id': value['binding']['source']['chat_id'],
                    'payload_hash': value['parts']['text']['payload_hash'],
                    'public_reply_sha256': hashlib.sha256(security.redact_outbound(text).encode()).hexdigest()}
        assert not isinstance(capability, dict)
        assert pf.finish(db, admission, 'text', result)
        receipt = pf.pending(db)[0]['parts']['text']['publication_receipt']
        assert receipt == expected
        from gateway.pending_final_publication import read
        with db._read_ctx() as conn:
            before = conn.total_changes
            assert read(conn, tmp_path, value['binding']) == expected
            assert conn.total_changes == before
        assert (tmp_path / 'executions').read_text() == '1'
    finally:
        db.close()


@pytest.mark.asyncio
async def test_publication_verifier_binds_task_to_original_claimed_content(tmp_path, monkeypatch):
    from plugins.platforms.a2a import durable_tasks as dt
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path / 'user'))
    identity = public.seed(tmp_path)
    db = SessionDB(tmp_path / 'state.db')
    try:
        value = pf.pending(db)[0]
        admission = value['binding']['admission_id']
        text = value['parts']['text']['content']
        assert pf.claim(db, admission, 'text', text, recovering=True)[1]
        adapter = A2AAdapter(PlatformConfig(enabled=True, extra={'port': 0}))
        with strict_transport_scope():
            result = await adapter.send(value['binding']['source']['chat_id'], text,
                                        reply_to=identity['task'], metadata=value['metadata'])
        assert result.success, result.error
        claimed = pf.pending(db)[0]
        receipt = {'version': 1, 'platform': 'a2a', 'admission_id': admission,
                   'task_id': identity['task'], 'context_id': value['binding']['source']['chat_id'],
                   'payload_hash': value['parts']['text']['payload_hash'],
                   'public_reply_sha256': hashlib.sha256(security.redact_outbound(text).encode()).hexdigest()}
        key = dt._key(identity['task'])
        with db._read_ctx() as conn:
            original = conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()[0]
            assert dt.verify_publication(conn, tmp_path, receipt, claimed) is True
        changed = json.loads(original)
        changed['reply'] = 'not original sealed text'
        changed['public_reply_sha256'] = hashlib.sha256(changed['reply'].encode()).hexdigest()
        db._execute_write(lambda conn: conn.execute('UPDATE state_meta SET value=? WHERE key=?',
                                                   (json.dumps(changed), key)))
        forged = deepcopy(receipt)
        forged['public_reply_sha256'] = changed['public_reply_sha256']
        with db._read_ctx() as conn:
            before = conn.total_changes
            assert dt.verify_publication(conn, tmp_path, receipt, claimed) is False
            assert dt.verify_publication(conn, tmp_path, forged, claimed) is False
            assert conn.total_changes == before
        assert (tmp_path / 'executions').read_text() == '1'
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['body_hash', 'result_shape', 'plain_dict', 'recreated_capability', 'lost_ack'])
async def test_ack_refuses_publication_drift_or_forged_proof_without_redelivery(tmp_path, monkeypatch, fault):
    from plugins.platforms.a2a import durable_tasks as dt
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path / 'user'))
    identity = public.seed(tmp_path)
    db = SessionDB(tmp_path / 'state.db')
    try:
        value = pf.pending(db)[0]
        admission = value['binding']['admission_id']
        text = value['parts']['text']['content']
        assert pf.claim(db, admission, 'text', text, recovering=True)[1]
        adapter = A2AAdapter(PlatformConfig(enabled=True, extra={'port': 0}))
        with strict_transport_scope():
            result = await adapter.send(value['binding']['source']['chat_id'], text,
                                        reply_to=identity['task'], metadata=value['metadata'])
        assert result.success, result.error
        if fault in {'body_hash', 'result_shape'}:
            key = dt._key(identity['task'])
            def damage(conn):
                task = json.loads(conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()[0])
                if fault == 'body_hash':
                    task['reply'] = 'not originally published'
                    task['public_reply_sha256'] = hashlib.sha256(task['reply'].encode()).hexdigest()
                if fault == 'result_shape':
                    task['durable_result_v1'] = [task['durable_result_v1']]
                conn.execute('UPDATE state_meta SET value=? WHERE key=?', (json.dumps(task), key))
            db._execute_write(damage)
        if fault == 'plain_dict':
            capability = result.raw_response['publication_receipt_v1']
            result.raw_response['publication_receipt_v1'] = json.loads(capability.encoded)
        if fault == 'recreated_capability':
            result.raw_response['publication_receipt_v1'] = replace(result.raw_response['publication_receipt_v1'])
        if fault == 'lost_ack':
            result.success = False
            result.error = 'accepted public write with unknown ACK'
        pf.finish(db, admission, 'text', result)
        part = pf.pending(db)[0]['parts']['text']
        assert part['state'] == 'unknown' and part['attempts'] == 1
        assert 'publication_receipt' not in part and 'delivered_at' not in part
        assert public.recover(tmp_path, 'recover')['delivered'] == 0
        assert (tmp_path / 'executions').read_text() == '1'
    finally:
        db.close()
