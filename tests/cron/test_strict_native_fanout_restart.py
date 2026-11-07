import asyncio
import json

import pytest

from cron import delivery_queue as queue, scheduler
from tests.cron.test_strict_native_fanout import consume, produce
from tests.gateway import strict_native_thread_support as support


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', ['discord', 'telegram', 'matrix'])
@pytest.mark.parametrize('stop,status,mutations', [
    ('after_claim', 'delivered', 2),
    ('after:0:create_claimed', 'unknown', 0),
    ('before:0:root_acked', 'unknown', 1),
    ('after:0:root_acked', 'delivered', 2),
    ('after:0:body_claimed', 'unknown', 1),
    ('before:0:done', 'unknown', 2),
    ('after:0:done', 'delivered', 2)])
async def test_native_claim_root_body_boundaries_across_fresh_consumers(tmp_path, monkeypatch, platform,
                                                                      stop, status, mutations):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(queue, 'DELIVERY_DB', tmp_path / 'cron' / 'deliveries.db')
    monkeypatch.setattr(scheduler, 'load_config', lambda: {'cron': {'wrap_response': False}})
    await asyncio.to_thread(produce, tmp_path, platform)
    async with support.peer(platform) as wire:
        await consume(tmp_path, platform, wire.base, stop)
        first = await consume(tmp_path, platform, wire.base)
        trace = list(wire.events)
        second = await consume(tmp_path, platform, wire.base)
    assert first['row']['status'] == second['row']['status'] == status
    assert wire.events == trace
    assert len([event for event in wire.events if event['method'] != 'GET']) == mutations
    assert (tmp_path / 'native-producer-calls').read_text().splitlines() == ['executed']
    audit = json.loads(second['row']['thread_receipts_json'])
    assert audit['items'][0]['platform'] == platform
    root_saved = stop not in {'after:0:create_claimed', 'before:0:root_acked'}
    assert audit['items'][0]['root'] == (('$root' if platform == 'matrix' else '777') if root_saved else None)
    assert second['row']['job_json'] == '{}' and second['row']['content'] == ''
    assert 'SEALED_NATIVE_CRON_RESULT' not in second['row']['thread_receipts_json']
    if status == 'delivered':
        transcript = [entry['transcript'] for entry in first['sessions'].values()]
        assert any('SEALED_NATIVE_CRON_RESULT' in str(messages) for messages in transcript)


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', ['discord', 'telegram', 'matrix'])
@pytest.mark.parametrize('faults,mutations', [(['lost'], 1), ([None, 'lost'], 2)])
async def test_native_accepted_lost_ack_never_recreates_or_resends(tmp_path, monkeypatch, platform, faults, mutations):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(queue, 'DELIVERY_DB', tmp_path / 'cron' / 'deliveries.db')
    monkeypatch.setattr(scheduler, 'load_config', lambda: {'cron': {'wrap_response': False}})
    await asyncio.to_thread(produce, tmp_path, platform)
    if platform == 'matrix' and len(faults) == 2:
        faults = [None, None, 'lost']
    async with support.peer(platform, faults) as wire:
        first = await consume(tmp_path, platform, wire.base)
        trace = list(wire.events)
        second = await consume(tmp_path, platform, wire.base)
    assert first['row']['status'] == second['row']['status'] == 'unknown'
    assert wire.events == trace
    assert len([event for event in wire.events if event['method'] != 'GET']) == mutations
    assert (tmp_path / 'native-producer-calls').read_text().splitlines() == ['executed']
