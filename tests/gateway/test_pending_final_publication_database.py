"""Follow-up probes limited to newly unresolved API03 trust boundaries."""
import hashlib
import json
import sqlite3

import pytest

from gateway import pending_final as pf
from gateway import pending_final_publication as pub
from gateway.platforms.base import SendResult
from hermes_state import SessionDB
from tests.gateway.test_pending_final_publication_receipt import prepared_claim, publication


@pytest.mark.asyncio
async def test_ack_capability_must_not_cross_actual_database_with_same_home(tmp_path):
    db, row, _value = await prepared_claim(tmp_path)
    try:
        cap, _ = publication(db, row)
        other_path = tmp_path / 'other.db'
        with db._read_ctx() as source, sqlite3.connect(other_path) as target:
            source.backup(target)
        with SessionDB(other_path) as other:
            accepted = pf.finish(other, row['admission_id'], 'text', SendResult(success=True, message_id='task',
                raw_response={'publication_receipt_v1': cap}))
            other_part = pf.pending(other)[0]['parts']['text']
            original_part = pf.pending(db)[0]['parts']['text']
            print(json.dumps({'actual_capture_db': str(db.db_path), 'actual_ack_db': str(other.db_path),
                'same_home': db.db_path.parent == other.db_path.parent, 'ack_accepted': accepted,
                'other_state': other_part['state'], 'other_has_receipt': 'publication_receipt' in other_part,
                'original_state': original_part['state']}))
            assert not accepted, 'issued capability crossed its actual SQLite database boundary during ACK'
            assert other_part['state'] != 'delivered' and 'publication_receipt' not in other_part
    finally:
        db.close()


@pytest.mark.asyncio
async def test_expiry_keeps_independent_original_hash_despite_current_public_row_drift(tmp_path, monkeypatch):
    db, row, value = await prepared_claim(tmp_path)
    try:
        cap, _ = publication(db, row)
        assert pf.finish(db, row['admission_id'], 'text', SendResult(success=True, message_id='task',
                         raw_response={'publication_receipt_v1': cap}))
        original = pf.pending(db)[0]['parts']['text']['publication_receipt']
        changed = dict(original, public_reply_sha256=hashlib.sha256(b'changed public').hexdigest())
        db._execute_write(lambda c: c.execute('UPDATE state_meta SET value=? WHERE key=?',
            (json.dumps(changed), 'test.publication:' + row['admission_id'])))
        monkeypatch.setattr(pf.time, 'time', lambda: value['created_at'] + pf.RETENTION_SECONDS + 10)
        monkeypatch.setattr('gateway.delivery_ledger._owner_alive', lambda *args: False)
        pf.reconcile_orphans(db)
        current = pf.pending(db)[0]
        assert current['expired'] and 'content' not in current['parts']['text']
        with db._read_ctx() as conn:
            before = conn.total_changes
            read = pub.read(conn, tmp_path, current['binding'])
            assert read == original and read['public_reply_sha256'] != changed['public_reply_sha256']
            assert conn.total_changes == before
        print(json.dumps({'content_removed': True, 'original_hash_preserved': True,
                          'current_row_hash_cannot_replace_original': True}))
    finally:
        db.close()


@pytest.mark.asyncio
async def test_current_row_only_callback_cannot_prove_original_content_relationship(tmp_path):
    db, row, value = await prepared_claim(tmp_path)
    try:
        key = 'review.weak-publication:' + row['admission_id']
        arbitrary_public = 'unrelated to sealed final'
        digest = hashlib.sha256(arbitrary_public.encode()).hexdigest()
        def weak_verify(conn, receipt, sealed):
            saved = conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()
            body = json.loads(saved[0]) if saved else {}
            return body.get('hash') == receipt['public_reply_sha256'] == hashlib.sha256(body.get('reply', '').encode()).hexdigest()
        def publish(conn):
            conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)',
                (key, json.dumps({'reply': arbitrary_public, 'hash': digest})))
            return pub.capture(conn, tmp_path, row['admission_id'], 'text', task_id='task', context_id='context',
                               public_reply=arbitrary_public, verify=weak_verify)
        cap = db._execute_write(publish)
        assert pf.finish(db, row['admission_id'], 'text', SendResult(success=True, message_id='task',
                         raw_response={'publication_receipt_v1': cap}))
        proof = pf.pending(db)[0]['parts']['text']['publication_receipt']
        assert proof['public_reply_sha256'] == digest
        print(json.dumps({'weak_callback_accepted': True, 'original_content': value['parts']['text']['content'],
            'public_reply': arbitrary_public, 'classification': 'trusted_callback_contract_dependency_not_remote_forgery'}))
    finally:
        db.close()
