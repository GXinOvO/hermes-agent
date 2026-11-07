import asyncio

import pytest

from cron import delivery_queue as queue, scheduler
from tests.cron.test_strict_native_fanout import consume, produce
from tests.gateway import strict_native_thread_support as support


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', ['discord', 'telegram', 'matrix'])
@pytest.mark.parametrize('mode', ['normal', 'unauthorized', 'reset', 'ended'])
async def test_native_inbound_uses_exact_seed_and_preserves_session_boundaries(tmp_path, monkeypatch, platform, mode):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(queue, 'DELIVERY_DB', tmp_path / 'cron' / 'deliveries.db')
    monkeypatch.setattr(scheduler, 'load_config', lambda: {'cron': {'wrap_response': False}})
    await asyncio.to_thread(produce, tmp_path, platform)
    async with support.peer(platform) as wire:
        posted = await consume(tmp_path, platform, wire.base)
        inbound = await consume(tmp_path, platform, wire.base, 'inbound:' + mode)
    assert posted['row']['status'] == inbound['row']['status'] == 'delivered'
    result = inbound['inbound']
    if mode == 'unauthorized':
        assert not result['routes'] and any(item['allowed'] is False for item in result['auth'])
    else:
        assert len(result['routes']) == 1
        route = result['routes'][0]
        assert route['key'] == result['seeded_key']
        if mode == 'normal':
            assert route['id'] == result['seeded']
            assert 'SEALED_NATIVE_CRON_RESULT' in str(route['transcript'])
        else:
            assert route['id'] != result['seeded']
            assert 'SEALED_NATIVE_CRON_RESULT' not in str(route['transcript'])
    assert (tmp_path / 'native-producer-calls').read_text().splitlines() == ['executed']
