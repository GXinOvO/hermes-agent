from copy import deepcopy
from dataclasses import replace
import hashlib
import json

import pytest

from gateway import pending_final as pf
from gateway.platforms.base import SendResult
from gateway.config import Platform
from tests.gateway import test_pending_final as fixtures
from tests.gateway.test_pending_final_original_identity import settle


async def prepared_claim(tmp_path):
    db, epoch, row, event, _, authority = await fixtures._sealed(tmp_path, human=True)
    payload = deepcopy(row['payload'])
    payload['native_text_v1']['source'].update(platform='a2a', chat_id='context')
    db._execute_write(lambda conn: conn.execute('UPDATE session_admissions SET payload_json=? WHERE admission_id=?',
                                                (json.dumps(payload), row['admission_id'])))
    row['payload'] = payload
    event.source.platform = Platform('a2a')
    event.source.chat_id = 'context'
    event.reply_anchor_override = 'task'
    descriptor = await pf.prepare(authority, row, event, payload['native_text_v1']['route'], 'sealed final')
    settle(db, epoch, row, descriptor)
    assert pf.claim(db, row['admission_id'], 'text', 'sealed final')[1]
    return db, row, pf.pending(db)[0]


def publication(db, row):
    from gateway import pending_final_publication as pub
    key = 'test.publication:' + row['admission_id']
    def verify(conn, receipt, sealed):
        raw = conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()
        return (raw is not None and json.loads(raw[0]) == receipt
                and sealed['parts']['text']['content'] == 'sealed final'
                and receipt['public_reply_sha256'] == hashlib.sha256(b'public final').hexdigest())
    def publish(conn):
        value = pf._read(conn, row['admission_id'])
        expected = {'version': 1, 'platform': 'a2a', 'admission_id': row['admission_id'],
            'task_id': 'task', 'context_id': 'context', 'payload_hash': value['parts']['text']['payload_hash'],
            'public_reply_sha256': hashlib.sha256(b'public final').hexdigest()}
        conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)', (key, json.dumps(expected)))
        return pub.capture(conn, db.db_path.parent, row['admission_id'], 'text', task_id='task',
                           context_id='context', public_reply='public final', verify=verify)
    return db._execute_write(publish), verify


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['none', 'plain_dict', 'copied', 'task', 'context', 'payload', 'epoch', 'failure',
                                  'publication_row', 'message_id', 'foreign_db', 'foreign_home'])
async def test_publication_receipt_requires_real_claim_and_runtime_capability(tmp_path, fault):
    from gateway import pending_final_publication as pub
    db, row, value = await prepared_claim(tmp_path)
    try:
        capability, verify = publication(db, row)
        candidate = capability
        if fault == 'plain_dict':
            candidate = json.loads(capability.encoded)
        if fault == 'copied':
            candidate = replace(capability, encoded=capability.encoded.replace('public_reply_sha256', 'foreign'))
        if fault == 'task':
            with pytest.raises(ValueError):
                db._execute_write(lambda conn: pub.capture(conn, tmp_path, row['admission_id'], 'text',
                    task_id='foreign', context_id='context', public_reply='public final', verify=verify))
            return
        if fault == 'context':
            with pytest.raises(ValueError):
                db._execute_write(lambda conn: pub.capture(conn, tmp_path, row['admission_id'], 'text',
                    task_id='task', context_id='foreign', public_reply='public final', verify=verify))
            return
        if fault == 'payload':
            def mutate(conn):
                current = pf._read(conn, row['admission_id'])
                current['parts']['text']['content'] = 'foreign'
                current['parts']['text']['payload_hash'] = pf._digest('foreign')
                pf._put(conn, row['admission_id'], current)
            db._execute_write(mutate)
        if fault == 'epoch':
            db._execute_write(lambda conn: conn.execute('UPDATE runtime_epoch SET epoch=epoch+1'))
        if fault == 'publication_row':
            db._execute_write(lambda conn: conn.execute('UPDATE state_meta SET value=? WHERE key=?',
                ('{}', 'test.publication:' + row['admission_id'])))
        if fault == 'foreign_db':
            from hermes_state import SessionDB
            with SessionDB(tmp_path / 'foreign' / 'state.db') as foreign:
                with foreign._read_ctx() as conn, pytest.raises(ValueError):
                    pub.capture(conn, tmp_path, row['admission_id'], 'text', task_id='task',
                                context_id='context', public_reply='public final', verify=verify)
            return
        if fault == 'foreign_home':
            with pytest.raises(ValueError):
                db._execute_write(lambda conn: pub.capture(conn, tmp_path / 'foreign', row['admission_id'], 'text',
                    task_id='task', context_id='context', public_reply='public final', verify=verify))
            return
        result = SendResult(success=fault != 'failure', message_id='foreign' if fault == 'message_id' else 'task',
                            raw_response={'publication_receipt_v1': candidate})
        if fault == 'epoch':
            with pytest.raises(RuntimeError, match='epoch'):
                pf.finish(db, row['admission_id'], 'text', result)
            return
        accepted = pf.finish(db, row['admission_id'], 'text', result)
        current = pf.pending(db)[0]
        part = current['parts']['text']
        if fault == 'none':
            assert accepted and part['state'] == 'delivered'
            expected = {'version': 1, 'platform': 'a2a', 'admission_id': row['admission_id'],
                'task_id': 'task', 'context_id': 'context', 'payload_hash': value['parts']['text']['payload_hash'],
                'public_reply_sha256': hashlib.sha256(b'public final').hexdigest()}
            assert part['publication_receipt'] == expected
            with db._read_ctx() as conn:
                assert pub.read(conn, tmp_path, current['binding']) == expected
        else:
            assert part['state'] == 'unknown' and 'publication_receipt' not in part
            assert 'delivered_at' not in part
    finally:
        db.close()


@pytest.mark.asyncio
async def test_publication_receipt_survives_content_expiry_without_reconstruction(tmp_path, monkeypatch):
    from gateway import pending_final_publication as pub
    db, row, value = await prepared_claim(tmp_path)
    try:
        capability, _ = publication(db, row)
        assert pf.finish(db, row['admission_id'], 'text', SendResult(success=True, message_id='task',
                         raw_response={'publication_receipt_v1': capability}))
        original = pf.pending(db)[0]['parts']['text']['publication_receipt']
        monkeypatch.setattr(pf.time, 'time', lambda: value['created_at'] + pf.RETENTION_SECONDS + 10)
        monkeypatch.setattr('gateway.delivery_ledger._owner_alive', lambda *a: False)
        pf.reconcile_orphans(db)
        current = pf.pending(db)[0]
        assert current['expired'] and 'content' not in current['parts']['text']
        assert current['parts']['text']['publication_receipt'] == original
        with db._read_ctx() as conn:
            assert pub.read(conn, tmp_path, current['binding']) == original
        def strip(conn):
            current['parts']['text'].pop('publication_receipt')
            pf._put(conn, row['admission_id'], current)
        db._execute_write(strip)
        with db._read_ctx() as conn:
            before = conn.total_changes
            assert pub.read(conn, tmp_path, current['binding']) is None
            assert conn.total_changes == before
    finally:
        db.close()
