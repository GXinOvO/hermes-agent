import pytest

from gateway import pending_final as pf
from gateway.session_results import finish_result
from hermes_state_terminal import RESULT_PREFIX
from tests.gateway import test_pending_final as fixtures


@pytest.mark.asyncio
@pytest.mark.parametrize('raw', ['null', '[]', '7', '"wrong result"', '{'])
async def test_corrupt_identity_result_is_rejected_and_preserved_before_claim(tmp_path, raw):
    db, epoch, row, _, descriptor, _ = await fixtures._sealed(tmp_path, human=True)
    try:
        finish_result(db, epoch=epoch, row=row, response='sealed final', outcome='completed',
            result={'result': {'final_response': 'sealed final', 'messages': [], '_pending_final_v1': descriptor}, 'usage': {}})
        db._execute_write(lambda conn: conn.execute('UPDATE state_meta SET value=? WHERE key=?',
                                                   (raw, RESULT_PREFIX + row['admission_id'])))
        value = pf.pending(db)[0]
        with db._read_ctx() as conn:
            before = conn.total_changes
            with pytest.raises(ValueError, match='identity witness'):
                pf.read_identity_witness(conn, tmp_path, value['binding'])
            assert conn.total_changes == before
            assert conn.execute('SELECT value FROM state_meta WHERE key=?', (RESULT_PREFIX + row['admission_id'],)).fetchone()[0] == raw
        current, won, reason = pf.claim(db, row['admission_id'], 'text', 'sealed final')
        assert not won and reason == 'invalidated'
        assert current['parts']['text']['attempts'] == 0
    finally:
        db.close()
