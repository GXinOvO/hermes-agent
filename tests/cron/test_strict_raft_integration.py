import asyncio

import pytest

from cron import delivery_queue as queue, scheduler
from gateway.config import GatewayConfig, Platform, PlatformConfig
from plugins.platforms.raft.adapter import RaftAdapter
from tests.gateway import test_raft_strict_native_transport as transport


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['sent', 'drop'])
@pytest.mark.parametrize('targets', [1, 2])
async def test_raft_exact_channel_cron_uses_generic_prepared_binding(tmp_path, monkeypatch, mode, targets):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(queue, 'DELIVERY_DB', tmp_path / 'cron' / 'deliveries.db')
    monkeypatch.setattr('cron.delivery_outcome.settle_quietly', lambda *args: None)
    monkeypatch.setattr(scheduler, 'load_config', lambda: {'cron': {'wrap_response': False, 'mirror_to_session': False}})
    async with transport.peer(mode) as wire:
        transport.credential(tmp_path, monkeypatch, wire.url)
        adapter = RaftAdapter(PlatformConfig(enabled=True))
        monkeypatch.setattr('gateway.config.load_gateway_config',
            lambda: GatewayConfig(platforms={Platform('raft'): adapter.config}))
        channels = ['#loopback', '#other'][:targets]
        job = {'id': 'raft-cron', 'execution_id': 'raft-execution',
               'deliver': ','.join('raft:' + channel for channel in channels)}
        queue.enqueue('raft-execution', job, 'sealed-raft-cron-result')
        assert await asyncio.to_thread(scheduler.drain_delivery_queue,
            {Platform('raft'): adapter}, asyncio.get_running_loop()) == 1
        row = queue.get_status('raft-execution')
        assert row['status'] == ('delivered' if mode == 'sent' else 'unknown')
        expected = channels if mode == 'sent' else channels[:1]
        assert [event['body']['target'] for event in wire.events] == expected
        assert all(event['body']['content'] == 'sealed-raft-cron-result' for event in wire.events)
        assert all(event['path'] == '/internal/agent-api/v2/send' for event in wire.events)
        assert await asyncio.to_thread(scheduler.drain_delivery_queue,
            {Platform('raft'): adapter}, asyncio.get_running_loop()) == 0
        assert len(wire.events) == len(expected)
