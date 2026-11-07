from dataclasses import dataclass

import pytest

from gateway.platforms.base import SendResult
from gateway.strict_transport import prepare_strict_transport, single_attempt_transport, strict_call, strict_transport
from tests.gateway import frozen_live_http_peer as native


@dataclass(eq=False)
class Plan:
    owner: object
    body: bytes
    execution: str
    claim: str
    used: bool = False


class Adapter:
    def __init__(self, endpoint):
        self.endpoint = endpoint
        self.prepared = []
        self.calls = []

    async def _prepare_strict_transport(self, method, *, chat_id, content, execution_id, claim_id, **kwargs):
        assert not strict_transport()
        assert method == 'send'
        plan = Plan(self, (chat_id + ':' + content).encode(), execution_id, claim_id)
        self.prepared.append(plan)
        return plan

    @single_attempt_transport
    async def send(self, *, chat_id, content, prepared_plan=None, execution_id=None, claim_id=None):
        assert strict_transport()
        plan = prepared_plan
        if (plan is None or plan.owner is not self or plan.used or plan.body != (chat_id + ':' + content).encode()
                or (execution_id, claim_id) != (plan.execution, plan.claim)):
            return SendResult(success=False, error='plan binding refused')
        plan.used = True
        self.calls.append(plan)
        import aiohttp
        async with aiohttp.ClientSession(trust_env=False) as session:
            async with session.post(self.endpoint, data=plan.body, allow_redirects=False) as response:
                await response.read()
                return SendResult(success=response.status == 200)


@pytest.mark.asyncio
async def test_generic_prepare_returns_exact_opaque_plan():
    async with native.peer() as wire:
        adapter = Adapter(wire.base + '/once')
        plan = await prepare_strict_transport(adapter, chat_id='target', content='sealed', execution_id='exec-A', claim_id='claim-A')
        assert plan is adapter.prepared[0]
        assert not native.writes(wire)


@pytest.mark.asyncio
async def test_explicit_opaque_plan_is_not_prepared_again_and_ids_reach_native_send():
    async with native.peer() as wire:
        adapter = Adapter(wire.base + '/once')
        plan = await adapter._prepare_strict_transport('send', chat_id='target', content='sealed', execution_id='exec-A', claim_id='claim-A')
        result = await strict_call(adapter, chat_id='target', content='sealed', prepared_plan=plan,
                                   execution_id='exec-A', claim_id='claim-A')
        assert result.success
        assert adapter.prepared == adapter.calls == [plan]
        assert [event.body for event in native.writes(wire)] == [b'target:sealed']
        assert not (await strict_call(adapter, chat_id='target', content='sealed', prepared_plan=plan,
                                      execution_id='exec-A', claim_id='claim-A')).success
        assert len(native.writes(wire)) == 1


@pytest.mark.asyncio
async def test_two_independent_generic_plans_keep_their_claim_and_payload():
    async with native.peer() as wire:
        adapter = Adapter(wire.base + '/once')
        plans = [await adapter._prepare_strict_transport('send', chat_id='target-' + key, content='sealed-' + key,
                 execution_id='exec-' + key, claim_id='claim-' + key) for key in ('A', 'B')]
        for key, plan in zip(('B', 'A'), reversed(plans)):
            result = await strict_call(adapter, chat_id='target-' + key, content='sealed-' + key,
                                       prepared_plan=plan, execution_id='exec-' + key, claim_id='claim-' + key)
            assert result.success
        assert len(adapter.prepared) == 2
        assert [event.body for event in native.writes(wire)] == [b'target-B:sealed-B', b'target-A:sealed-A']


@pytest.mark.asyncio
async def test_explicit_no_plan_legacy_path_has_no_after_claim_preparation():
    class Legacy:
        def __init__(self):
            self.prepares = 0

        async def _prepare_strict_transport(self, method, **kwargs):
            self.prepares += 1

        @single_attempt_transport
        async def send(self, *, chat_id, content):
            return SendResult(success=strict_transport())

    adapter = Legacy()
    assert await prepare_strict_transport(adapter, chat_id='target', content='sealed') is None
    assert (await strict_call(adapter, chat_id='target', content='sealed', prepared_plan=None)).success
    assert adapter.prepares == 1
