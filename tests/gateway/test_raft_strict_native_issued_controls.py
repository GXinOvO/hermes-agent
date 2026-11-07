import asyncio
from copy import copy
from dataclasses import replace
import gc

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.raft.adapter import RaftAdapter
from tests.gateway import test_raft_strict_native_transport as transport


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['shallow_copy', 'replace_record', 'mutate_record', 'reset_consumed'])
async def test_issued_plan_integrity_remains_bound_at_consumption(fault, tmp_path, monkeypatch):
    async with transport.peer() as wire:
        transport.credential(tmp_path, monkeypatch, wire.url)
        adapter = RaftAdapter(PlatformConfig(enabled=True))
        plan = await transport.prepare(adapter)
        expected_posts = 0
        if fault == 'shallow_copy':
            plan = copy(plan)
        elif fault == 'replace_record':
            plan._frozen = replace(plan._frozen, token='sk_agent_foreign-inert-only')
        elif fault == 'mutate_record':
            object.__setattr__(plan._frozen, 'body', b'changed')
        else:
            assert (await transport.consume(adapter, plan)).success
            expected_posts = 1
            plan._used = False
        with pytest.raises(RuntimeError, match='plan|consum|foreign'):
            await transport.consume(adapter, plan)
        assert len(wire.events) == expected_posts


@pytest.mark.asyncio
async def test_independent_issued_plans_for_one_claim_share_consumption(tmp_path, monkeypatch):
    async with transport.peer() as wire:
        transport.credential(tmp_path, monkeypatch, wire.url)
        adapter = RaftAdapter(PlatformConfig(enabled=True))
        plans = await asyncio.gather(transport.prepare(adapter), transport.prepare(adapter))
        outcomes = await asyncio.gather(*(transport.consume(adapter, plan) for plan in plans), return_exceptions=True)
        assert sum(getattr(result, 'success', False) for result in outcomes) == 1
        assert len(wire.events) == 1


@pytest.mark.asyncio
async def test_consumed_claim_survives_original_plan_release(tmp_path, monkeypatch):
    async with transport.peer() as wire:
        transport.credential(tmp_path, monkeypatch, wire.url)
        adapter = RaftAdapter(PlatformConfig(enabled=True))
        plan = await transport.prepare(adapter)
        assert (await transport.consume(adapter, plan)).success
        del plan
        gc.collect()
        try:
            another = await transport.prepare(adapter)
            result = await transport.consume(adapter, another)
        except (RuntimeError, ValueError):
            result = None
        assert not getattr(result, 'success', False)
        assert len(wire.events) == 1


@pytest.mark.asyncio
async def test_same_claim_cannot_be_issued_with_another_payload(tmp_path, monkeypatch):
    async with transport.peer() as wire:
        transport.credential(tmp_path, monkeypatch, wire.url)
        adapter = RaftAdapter(PlatformConfig(enabled=True))
        original = await transport.prepare(adapter)
        with pytest.raises((RuntimeError, ValueError), match='claim|binding'):
            await transport.prepare(adapter, content='different completed result')
        assert (await transport.consume(adapter, original)).success
        assert len(wire.events) == 1


@pytest.mark.asyncio
async def test_claim_capacity_refuses_new_plan_without_reviving_consumed_identity(tmp_path, monkeypatch):
    from plugins.platforms.raft import strict_delivery

    monkeypatch.setattr(strict_delivery, '_MAX_CLAIMS', 1)
    async with transport.peer() as wire:
        transport.credential(tmp_path, monkeypatch, wire.url)
        adapter = RaftAdapter(PlatformConfig(enabled=True))
        plan = await transport.prepare(adapter)
        assert (await transport.consume(adapter, plan)).success
        with pytest.raises(ValueError, match='capacity'):
            await transport.prepare(adapter, execution='another', claim='another:text')
        del plan
        gc.collect()
        with pytest.raises(ValueError, match='consumed'):
            await transport.prepare(adapter)
        assert len(wire.events) == 1
