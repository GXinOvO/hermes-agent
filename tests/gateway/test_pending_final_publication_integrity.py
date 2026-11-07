import json
import os
import sqlite3

import pytest

from gateway import pending_final as pf
from gateway import pending_final_publication as publication
from gateway.platforms.base import SendResult
from hermes_state import SessionDB
from hermes_state_terminal import RESULT_PREFIX, load_result
from tests.gateway.test_pending_final_publication_receipt import prepared_claim, publication as capture


def erase_identity(db, row):
    def erase(conn):
        value = pf._read(conn, row['admission_id'])
        value.pop('identity_witness_version')
        pf._put(conn, row['admission_id'], value)
        key = RESULT_PREFIX + row['admission_id']
        saved = load_result(conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()[0])
        saved.pop('_pending_final_identity_v1')
        conn.execute('UPDATE state_meta SET value=? WHERE key=?', (json.dumps(saved), key))
    db._execute_write(erase)


@pytest.mark.asyncio
async def test_missing_identity_after_capture_refuses_new_publication_ack(tmp_path):
    db, row, _value = await prepared_claim(tmp_path)
    try:
        capability, _verify = capture(db, row)
        erase_identity(db, row)
        assert pf.finish(db, row['admission_id'], 'text', SendResult(success=True, message_id='task',
            raw_response={'publication_receipt_v1': capability})) is False
        part = pf.pending(db)[0]['parts']['text']
        assert part['state'] == 'unknown' and part['attempts'] == 1
        assert 'publication_receipt' not in part and 'delivered_at' not in part
    finally:
        db.close()


@pytest.mark.asyncio
async def test_legacy_ordinary_ack_keeps_prior_delivery_without_publication_proof(tmp_path):
    db, row, value = await prepared_claim(tmp_path)
    try:
        erase_identity(db, row)
        assert pf.finish(db, row['admission_id'], 'text', SendResult(success=True))
        part = pf.pending(db)[0]['parts']['text']
        assert part['state'] == 'delivered' and 'delivered_at' in part
        assert 'publication_receipt' not in part
        with db._read_ctx() as conn:
            assert publication.read(conn, tmp_path, value['binding']) is None
            assert pf.read_identity_witness(conn, tmp_path, value['binding']) is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_same_canonical_path_replaced_database_cannot_use_original_capability(tmp_path):
    db, row, _value = await prepared_claim(tmp_path)
    try:
        capability, _verify = capture(db, row)
        replacement = tmp_path / 'replacement.db'
        with db._read_ctx() as source, sqlite3.connect(replacement) as target:
            source.backup(target)
        original = tmp_path / 'original.db'
        os.replace(db.db_path, original)
        os.replace(replacement, db.db_path)
        with SessionDB(db.db_path) as other:
            assert pf.finish(other, row['admission_id'], 'text', SendResult(success=True, message_id='task',
                raw_response={'publication_receipt_v1': capability})) is False
            part = pf.pending(other)[0]['parts']['text']
            assert part['state'] == 'unknown' and 'publication_receipt' not in part
    finally:
        db.close()
