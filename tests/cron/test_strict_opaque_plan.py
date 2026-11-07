import asyncio
import hashlib

import pytest

from cron import delivery_queue as queue, scheduler
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import SendResult
from gateway.strict_transport import single_attempt_transport
from tests.gateway import frozen_live_http_peer as native
from tests.gateway import test_pending_final as legacy


class Adapter(legacy.Adapter):
    def __init__(self, endpoint):
        super().__init__()
        self.platform = Platform('line')
        self.config = PlatformConfig(enabled=True)
        self.endpoint = endpoint
        self.plans = []

    async def _prepare_strict_transport(self, method, *, execution_id=None, claim_id=None, **kwargs):
        assert queue.get_status('opaque-execution')['status'] == 'pending'
        assert execution_id == 'opaque-execution' and claim_id
        plan = {'kwargs': kwargs, 'execution_id': execution_id, 'claim_id': claim_id, 'used': False}
        self.plans.append(plan)
        return plan

    def _strict_transport_binding(self, plan):
        return {'endpoint': self.endpoint, 'target': plan['kwargs']['chat_id'],
                'body_sha256': hashlib.sha256(plan['kwargs']['content'].encode()).hexdigest(),
                'execution_id': plan['execution_id'], 'claim_id': plan['claim_id']}

    @single_attempt_transport
    async def send(self, chat_id, content, *, prepared_plan=None, execution_id=None, claim_id=None, **kwargs):
        plan = prepared_plan
        assert any(plan is issued for issued in self.plans) and not plan['used']
        assert (execution_id, claim_id) == (plan['execution_id'], plan['claim_id'])
        assert plan['kwargs'] == dict(chat_id=chat_id, content=content, **kwargs)
        assert queue.get_status('opaque-execution')['status'] == 'delivering'
        plan['used'] = True
        import aiohttp
        async with aiohttp.ClientSession(trust_env=False) as session:
            async with session.post(self.endpoint, json={'target': chat_id, 'text': content}, allow_redirects=False) as response:
                await response.read()
                return SendResult(success=response.status == 200)


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', [None, 'lost'])
@pytest.mark.parametrize('targets', [1, 2])
async def test_cron_passes_each_preclaim_opaque_plan_through_native_queue(tmp_path, monkeypatch, fault, targets):
    monkeypatch.setattr(queue, 'DELIVERY_DB', tmp_path / 'deliveries.db')
    monkeypatch.setattr('cron.delivery_outcome.settle_quietly', lambda *args: None)
    monkeypatch.setattr(scheduler, 'load_config', lambda: {'cron': {'wrap_response': False, 'mirror_to_session': False}})
    async with native.peer(lambda event, index: fault) as wire:
        adapter = Adapter(wire.base + '/once')
        monkeypatch.setattr('gateway.config.load_gateway_config',
            lambda: GatewayConfig(platforms={Platform('line'): adapter.config}))
        job = {'id': 'opaque-job', 'execution_id': 'opaque-execution',
               'deliver': ','.join('line:target-' + str(index) for index in range(targets))}
        queue.enqueue('opaque-execution', job, 'sealed-opaque-result')
        assert await asyncio.to_thread(scheduler.drain_delivery_queue, {Platform('line'): adapter}, asyncio.get_running_loop()) == 1
        row = queue.get_status('opaque-execution')
        assert row['status'] == ('delivered' if fault is None else 'unknown')
        assert len(native.writes(wire)) == (targets if fault is None else 1)
        assert [event.payload['target'] for event in native.writes(wire)] == ['target-' + str(i) for i in range(targets if fault is None else 1)]
        assert len(adapter.plans) == targets
        assert await asyncio.to_thread(scheduler.drain_delivery_queue, {Platform('line'): adapter}, asyncio.get_running_loop()) == 0
