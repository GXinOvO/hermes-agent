"""One unresolved reader-side counterpart of the actual-main-DB boundary."""
import json
import sqlite3

import pytest

from gateway import pending_final as pf
from gateway import pending_final_publication as pub
from gateway.platforms.base import SendResult
from hermes_state import SessionDB
from tests.gateway.test_pending_final_publication_receipt import prepared_claim, publication


@pytest.mark.asyncio
async def test_publication_reader_rejects_other_database_even_when_logical_rows_match(tmp_path):
    db, row, value = await prepared_claim(tmp_path)
    try:
        cap, _ = publication(db, row)
        assert pf.finish(db, row['admission_id'], 'text', SendResult(success=True, message_id='task',
                         raw_response={'publication_receipt_v1': cap}))
        other_path = tmp_path / 'other.db'
        with db._read_ctx() as source, sqlite3.connect(other_path) as target:
            source.backup(target)
        with SessionDB(other_path) as other:
            with other._read_ctx() as conn:
                before = conn.total_changes
                proof, error = None, None
                try:
                    proof = pub.read(conn, tmp_path, value['binding'])
                except ValueError as exc:
                    error = str(exc)
                assert conn.total_changes == before
            print(json.dumps({'reader_actual_database': str(other.db_path), 'declared_home': str(tmp_path),
                'receipt_returned': proof is not None, 'error': error, 'readonly': True}))
            assert error is not None, 'reader trusted a foreign actual database solely through its caller-supplied home'
    finally:
        db.close()
