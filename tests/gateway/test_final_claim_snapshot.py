import pytest

from gateway import pending_final as pf
from gateway.final_delivery import send_text
from gateway.platforms.base import SendResult
from gateway.session_delivery_policy import automation_delivery_scope
from gateway.session_results import finish_result
from hermes_state_runtime import begin_runtime_epoch
from tests.gateway import test_pending_final as legacy
from tests.gateway import frozen_live_http_peer as native
from tests.gateway.test_final_prepared_transport import Adapter
from tests.gateway.test_strict_bridges_recovery import sealed


@pytest.mark.asyncio
@pytest.mark.parametrize('drift', ['metadata', 'adapter_profile', 'epoch-after-claim'])
async def test_final_claim_context_or_epoch_drift_never_submits(tmp_path, monkeypatch, drift):
    async with native.peer() as wire:
        adapter = Adapter(wire.base + '/once')
        db, _row, event, descriptor, response = await sealed(tmp_path, 'slack', adapter, 'target')
        adapter.db = db
        original = pf.claim

        def race(db, admission, kind, content, **kwargs):
            if drift != 'epoch-after-claim':
                def mutate(conn):
                    value = pf._read(conn, admission)
                    value[drift] = {'thread_id': 'other'} if drift == 'metadata' else 'other-owner'
                    pf._put(conn, admission, value)
                db._execute_write(mutate)
            result = original(db, admission, kind, content, **kwargs)
            if drift == 'epoch-after-claim' and result[1]:
                begin_runtime_epoch(db, instance_id='successor')
            return result

        monkeypatch.setattr(pf, 'claim', race)
        try:
            result = await send_text(adapter, event, descriptor['binding']['route'], response,
                                     reply_to=descriptor['reply_to'], metadata=descriptor['metadata'])
            assert not result.success
            assert native.writes(wire) == []
            assert not adapter.prepared[0]['used']
            assert pf.pending(db)[0]['parts']['text']['state'] != 'delivered'
        finally:
            db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('drift', ['target-boundary', 'metadata'])
async def test_finish_transaction_refuses_invalidated_claim(tmp_path, drift):
    db, epoch, row, event, descriptor, authority = await legacy._sealed(tmp_path)
    try:
        async with automation_delivery_scope(authority, row, event):
            pass
        finish_result(db, epoch=epoch, row=row, response='sealed final', outcome='completed',
            result={'result': {'final_response': 'sealed final', 'messages': [], '_pending_final_v1': descriptor}, 'usage': {}})
        assert pf.claim(db, row['admission_id'], 'text', 'sealed final')[1]

        def invalidate(conn):
            if drift == 'target-boundary':
                from hermes_state_background import invalidate_target
                invalidate_target(conn, row['target_session_id'])
            else:
                value = pf._read(conn, row['admission_id'])
                value['metadata'] = {'thread_id': 'other'}
                pf._put(conn, row['admission_id'], value)
        db._execute_write(invalidate)
        assert pf.finish(db, row['admission_id'], 'text', SendResult(success=True)) is False
        assert pf.pending(db)[0]['parts']['text']['state'] != 'delivered'
    finally:
        db.close()
