import math

import pytest

from gateway import pending_final as pf
from gateway.platforms.base import SendResult
from gateway.session_results import finish_result
from tests.gateway import test_pending_final as fixtures


@pytest.mark.asyncio
@pytest.mark.parametrize('ack', [True, False])
async def test_ack_time_is_recorded_only_with_successful_finish_transaction(tmp_path, monkeypatch, ack):
    db, epoch, row, _, descriptor, _ = await fixtures._sealed(tmp_path)
    try:
        finish_result(db, epoch=epoch, row=row, response='sealed final', outcome='completed',
            result={'result': {'final_response': 'sealed final', 'messages': [], '_pending_final_v1': descriptor}, 'usage': {}})
        assert 'delivered_at' not in pf.pending(db)[0]['parts']['text']
        assert pf.claim(db, row['admission_id'], 'text', 'sealed final')[1]
        before = pf.time.time()
        with monkeypatch.context() as clock:
            clock.setattr(pf.time, 'time', lambda: before + 3)
            assert pf.finish(db, row['admission_id'], 'text', SendResult(success=ack))
        part = pf.pending(db)[0]['parts']['text']
        if ack:
            assert type(part['delivered_at']) is float and math.isfinite(part['delivered_at'])
            assert part['delivered_at'] == before + 3
        else:
            assert part['state'] == 'unknown' and 'delivered_at' not in part
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('stamp', [True, -1, float('nan'), float('inf'), 10**400])
async def test_invalid_ack_time_preserves_raw_without_exposing_valid_result(tmp_path, stamp):
    db, epoch, row, _, descriptor, _ = await fixtures._sealed(tmp_path)
    try:
        finish_result(db, epoch=epoch, row=row, response='sealed final', outcome='completed',
            result={'result': {'final_response': 'sealed final', 'messages': [], '_pending_final_v1': descriptor}, 'usage': {}})
        assert pf.claim(db, row['admission_id'], 'text', 'sealed final')[1]
        assert pf.finish(db, row['admission_id'], 'text', SendResult(success=True))
        def corrupt(conn):
            value = pf._read(conn, row['admission_id'])
            value['parts']['text']['delivered_at'] = stamp
            pf._put(conn, row['admission_id'], value)
            return conn.execute('SELECT value FROM state_meta WHERE key=?', (pf.PREFIX + row['admission_id'],)).fetchone()[0]
        raw = db._execute_write(corrupt)
        assert pf.pending(db) == []
        with db._read_ctx() as conn:
            assert conn.execute('SELECT value FROM state_meta WHERE key=?', (pf.PREFIX + row['admission_id'],)).fetchone()[0] == raw
    finally:
        db.close()


@pytest.mark.asyncio
async def test_legacy_delivered_without_ack_time_remains_readable_but_is_not_backdated(tmp_path):
    db, epoch, row, _, descriptor, _ = await fixtures._sealed(tmp_path)
    try:
        finish_result(db, epoch=epoch, row=row, response='sealed final', outcome='completed',
            result={'result': {'final_response': 'sealed final', 'messages': [], '_pending_final_v1': descriptor}, 'usage': {}})
        assert pf.claim(db, row['admission_id'], 'text', 'sealed final')[1]
        assert pf.finish(db, row['admission_id'], 'text', SendResult(success=True))
        def strip(conn):
            value = pf._read(conn, row['admission_id'])
            value['parts']['text'].pop('delivered_at', None)
            pf._put(conn, row['admission_id'], value)
        db._execute_write(strip)
        value = pf.pending(db)[0]
        assert value['parts']['text']['state'] == 'delivered'
        assert 'delivered_at' not in value['parts']['text']
    finally:
        db.close()
