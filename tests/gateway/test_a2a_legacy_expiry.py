import json
from types import SimpleNamespace

import pytest

from gateway import pending_final as pf
from hermes_state import SessionDB
from plugins.platforms.a2a import durable_tasks as dt
from plugins.platforms.a2a.adapter import A2AAdapter
from gateway.config import PlatformConfig
from tests.gateway.test_a2a_durable_public import recover, seed


@pytest.mark.parametrize('ack_time', [False, True])
def test_legacy_ack_can_expire_after_core_body_without_replaying(tmp_path, monkeypatch, ack_time):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    identity = seed(tmp_path)
    assert recover(tmp_path, 'recover')['delivered'] == 1
    with SessionDB(tmp_path / 'state.db') as db:
        value = pf.pending(db)[0]
        def old_ack(conn):
            part = value['parts']['text']
            part.pop('publication_receipt')
            if not ack_time:
                part.pop('delivered_at')
            pf._put(conn, value['binding']['admission_id'], value)
        db._execute_write(old_ack)
        clock = [dt.time.time() + 8 * 24 * 3600]
        monkeypatch.setattr(dt, 'time', SimpleNamespace(time=lambda: clock[0]))
        monkeypatch.setattr(pf, 'time', SimpleNamespace(time=lambda: clock[0]))
        pf.reconcile_orphans(db)
        value = pf.pending(db)[0]
        assert value['expired'] and 'content' not in value['parts']['text']
        expired = dt.maintain(tmp_path)
        if ack_time:
            assert expired == [identity['task']]
        else:
            assert expired == []
            clock[0] += 8 * 24 * 3600
            assert dt.maintain(tmp_path) == [identity['task']]
        with db._read_ctx() as conn:
            saved = conn.execute('SELECT value FROM state_meta WHERE key=?', (dt._key(identity['task']),)).fetchone()[0]
        assert json.loads(saved)['expired'] and 'reply' not in json.loads(saved)
        for _ in range(2):
            adapter = A2AAdapter(PlatformConfig(enabled=True, extra={'port': 0}))
            assert adapter.tasks.get(identity['task']) is None
            response = adapter._rpc_message_send('retry', {'message': {'messageId': 'one-input',
                'contextId': 'real-context', 'parts': [{'text': 'do the work once'}]}}, 'ip:127.0.0.1')
            assert 'error' in response
        assert (tmp_path / 'executions').read_text() == '1'
