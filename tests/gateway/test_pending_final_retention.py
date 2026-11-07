import time

import pytest

from gateway import pending_final as pf
from gateway.platforms.base import SendResult
from gateway.session_results import finish_result
from tests.gateway.test_pending_final import _sealed


@pytest.mark.asyncio
@pytest.mark.parametrize('corrupt', ['{', '{"binding":{},"parts":{"text":{"state":"attempting"}},"created_at":1}'])
async def test_corrupt_final_is_preserved_without_blocking_valid_sibling(tmp_path, corrupt):
    db, epoch, row, _event, descriptor, _authority = await _sealed(tmp_path)
    try:
        finish_result(db, epoch=epoch, row=row, response='sealed final', outcome='completed',
            result={'result':{'final_response':'sealed final', 'messages':[], '_pending_final_v1':descriptor}, 'usage':{}})
        db._execute_write(lambda conn:conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)', (pf.PREFIX + 'corrupt', corrupt)))
        pf.reconcile_orphans(db)
        assert [v['binding']['admission_id'] for v in pf.pending(db)] == [row['admission_id']]
        with db._read_ctx() as conn:
            assert conn.execute('SELECT value FROM state_meta WHERE key=?', (pf.PREFIX + 'corrupt',)).fetchone()[0] == corrupt
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['pending', 'attempting', 'unknown', 'delivered'])
async def test_payload_expiry_preserves_delivery_fact_and_live_owner(state, tmp_path, monkeypatch):
    db, epoch, row, _event, descriptor, _authority = await _sealed(tmp_path)
    try:
        finish_result(db, epoch=epoch, row=row, response='sealed final', outcome='completed',
            result={'result':{'final_response':'sealed final', 'messages':[], '_pending_final_v1':descriptor}, 'usage':{}})
        if state != 'pending':
            assert pf.claim(db, row['admission_id'], 'text', 'sealed final')[1]
            if state != 'attempting':
                pf.finish(db, row['admission_id'], 'text', SendResult(success=state == 'delivered'))
        def old(conn):
            value = pf._read(conn, row['admission_id'])
            value['created_at'] = time.time() - pf.RETENTION_SECONDS - 1
            pf._put(conn, row['admission_id'], value)
        db._execute_write(old)
        with monkeypatch.context() as live:
            live.setattr('gateway.delivery_ledger._owner_alive', lambda pid, birth:True)
            pf.reconcile_orphans(db)
        value = pf.pending(db)[0]
        if state in {'pending', 'attempting'}:
            assert not value.get('expired')
            assert value['parts']['text']['state'] == state
        with monkeypatch.context() as dead:
            dead.setattr('gateway.delivery_ledger._owner_alive', lambda pid, birth:False)
            pf.reconcile_orphans(db)
        value = pf.pending(db)[0]
        assert value['expired']
        assert value['parts']['text']['state'] == ('unknown' if state == 'attempting' else state)
        assert 'content' not in value['parts']['text'] and 'response' not in value
        assert pf.claim(db, row['admission_id'], 'text', 'sealed final', recovering=True)[1:] == (False, 'expired')
    finally:
        db.close()
