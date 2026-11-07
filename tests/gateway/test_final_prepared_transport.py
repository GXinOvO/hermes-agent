import json

import pytest

from gateway import pending_final as pf
from gateway.final_delivery import send_text
from gateway.platforms.base import SendResult
from gateway.strict_transport import single_attempt_transport
from tests.gateway import frozen_live_http_peer as native
from tests.gateway import test_pending_final as legacy
from tests.gateway.test_strict_bridges_recovery import sealed


class Adapter(legacy.Adapter):
    def __init__(self, endpoint):
        super().__init__()
        self.endpoint = endpoint
        self.db = None
        self.prepared = []

    async def _prepare_strict_transport(self, method, *, execution_id=None, claim_id=None, **kwargs):
        assert execution_id and claim_id, 'trusted execution and part claim required before preparing'
        assert pf.pending(self.db)[0]['parts']['text']['state'] == 'pending'
        plan = {'method': method, 'kwargs': kwargs, 'ids': (execution_id, claim_id), 'used': False}
        self.prepared.append(plan)
        return plan

    @single_attempt_transport
    async def send(self, *, prepared_plan=None, execution_id=None, claim_id=None, **kwargs):
        plan = prepared_plan
        assert plan is self.prepared[0] and not plan['used']
        assert plan['ids'] == (execution_id, claim_id) and plan['kwargs'] == kwargs
        assert pf.pending(self.db)[0]['parts']['text']['state'] == 'attempting'
        plan['used'] = True
        import aiohttp
        async with aiohttp.ClientSession(trust_env=False) as session:
            async with session.post(self.endpoint, json={'text': kwargs['content']}, allow_redirects=False) as response:
                await response.read()
                return SendResult(success=response.status == 200)


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', [None, 'lost'])
async def test_final_dispatch_passes_the_preclaim_opaque_plan_once(tmp_path, fault):
    async with native.peer(lambda event, index: fault) as wire:
        adapter = Adapter(wire.base + '/once')
        db, row, event, descriptor, response = await sealed(tmp_path, 'slack', adapter, 'target')
        adapter.db = db
        try:
            result = await send_text(adapter, event, descriptor['binding']['route'], response,
                                     reply_to=descriptor['reply_to'], metadata=descriptor['metadata'])
            assert result.success is (fault is None)
            assert len(native.writes(wire)) == 1
            assert native.writes(wire)[0].payload == {'text': response}
            plan = adapter.prepared[0]
            assert plan['used']
            assert row['admission_id'] in plan['ids'][0]
            assert 'text' in plan['ids'][1]
            first = json.dumps(pf.pending(db), sort_keys=True)
            second = await send_text(adapter, event, descriptor['binding']['route'], response,
                                     reply_to=descriptor['reply_to'], metadata=descriptor['metadata'])
            assert second.success is (fault is None)
            assert len(native.writes(wire)) == 1
            assert json.dumps(pf.pending(db), sort_keys=True) == first
        finally:
            db.close()
