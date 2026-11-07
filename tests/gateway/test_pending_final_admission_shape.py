from copy import deepcopy
import json

import pytest

from gateway import pending_final as pf
from tests.gateway import test_pending_final as fixtures
from tests.gateway.test_pending_final_original_identity import settle


@pytest.mark.asyncio
@pytest.mark.parametrize('shape', ['payload', 'native', 'source', 'automation', 'local_automation'])
async def test_bad_admission_shape_refuses_before_claim_and_preserves_native_raw(tmp_path, shape):
    db, epoch, row, _, descriptor, _ = await fixtures._sealed(tmp_path, human=True)
    try:
        settle(db, epoch, row, descriptor)
        value = pf.pending(db)[0]
        payload = deepcopy(row['payload'])
        if shape == 'payload':
            payload = []
        if shape == 'native':
            payload['native_text_v1'] = []
        if shape == 'source':
            payload['native_text_v1']['source'] = []
        if shape == 'automation':
            payload['native_text_v1']['automation'] = []
        if shape == 'local_automation':
            payload['local_automation_v1'] = []
        raw = json.dumps(payload)
        db._execute_write(lambda conn: conn.execute('UPDATE session_admissions SET payload_json=? WHERE admission_id=?',
                                                    (raw, row['admission_id'])))
        with db._read_ctx() as conn:
            before = conn.total_changes
            with pytest.raises(ValueError, match='identity witness'):
                pf.read_identity_witness(conn, tmp_path, value['binding'])
            assert pf._valid(conn, value, tmp_path) is False
            assert conn.total_changes == before
            assert conn.execute('SELECT payload_json FROM session_admissions WHERE admission_id=?',
                                (row['admission_id'],)).fetchone()[0] == raw
        current, won, reason = pf.claim(db, row['admission_id'], 'text', 'sealed final')
        assert not won and reason == 'invalidated'
        assert current['parts']['text']['attempts'] == 0
    finally:
        db.close()


@pytest.mark.asyncio
async def test_valid_admission_shape_keeps_normal_reader_and_claim(tmp_path):
    db, epoch, row, _, descriptor, _ = await fixtures._sealed(tmp_path, human=True)
    try:
        settle(db, epoch, row, descriptor)
        value = pf.pending(db)[0]
        with db._read_ctx() as conn:
            assert pf.read_identity_witness(conn, tmp_path, value['binding'])['binding'] == value['binding']
            assert pf._valid(conn, value, tmp_path)
        assert pf.claim(db, row['admission_id'], 'text', 'sealed final')[1]
    finally:
        db.close()
