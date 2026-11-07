"""A live HTTP subscriber must not emit a final retired after its Future resolves."""
import asyncio
import json
import threading

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.a2a.adapter import A2AAdapter
from tests.gateway import a2a_durable_peer as peer
from tests.gateway import test_a2a_durable_public as public
from tests.gateway import test_a2a_public_target_lifecycle as lifecycle


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['unchanged', 'rewind'])
async def test_live_subscribe_rechecks_target_after_resolved_reply(tmp_path, monkeypatch, operation):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path / 'user'))
    identity = public.seed(tmp_path)
    assert public.recover(tmp_path, 'recover')['delivered'] == 1
    adapter = A2AAdapter(PlatformConfig(enabled=True, extra={'port': 0}))
    resolved, release = threading.Event(), threading.Event()
    original = adapter._await_future
    def held(*args, **kwargs):
        answer = original(*args, **kwargs)
        assert answer[1] == 'sealed native answer'
        resolved.set()
        assert release.wait(15), 'subscriber release timed out'
        return answer
    monkeypatch.setattr(adapter, '_await_future', held)
    reader = None
    try:
        assert await adapter.connect()
        url = f'http://127.0.0.1:{adapter._httpd.server_port}/'
        reader = asyncio.create_task(asyncio.to_thread(peer.rpc, url, 'SubscribeToTask', {'taskId': identity['task']}))
        assert await asyncio.to_thread(resolved.wait, 10)
        mutation = await lifecycle.mutate(tmp_path, operation) if operation == 'rewind' else None
        release.set()
        wire = await asyncio.wait_for(reader, 15)
        expected = operation == 'unchanged'
        print('A2A_LIVE_SUBSCRIBE ' + json.dumps({'operation': operation, 'mutation': mutation,
            'visible': 'sealed native answer' in wire, 'wire': wire, 'executions': (tmp_path / 'executions').read_text()}))
        assert ('sealed native answer' in wire) is expected, wire
        assert (tmp_path / 'executions').read_text() == '1'
    finally:
        release.set()
        if reader is not None:
            await asyncio.gather(reader, return_exceptions=True)
        await adapter.disconnect()
