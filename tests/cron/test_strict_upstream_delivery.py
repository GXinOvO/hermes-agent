import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cron import delivery_queue as queue, scheduler, strict_delivery
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.session import SessionStore
from plugins.platforms.slack.adapter import SlackAdapter
from slack_sdk.web.async_client import AsyncWebClient
from tests.cron import test_strict_mixed_fanout as slack_peer
from tests.gateway import strict_native_thread_support as native


@pytest.mark.asyncio
@pytest.mark.parametrize(('job_wrap', 'global_wrap'), [(False, True), (True, False)])
async def test_queued_wrap_response_overrides_global(tmp_path, monkeypatch, job_wrap, global_wrap):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(queue, 'DELIVERY_DB', tmp_path / 'cron/deliveries.db')
    monkeypatch.setattr(scheduler, 'load_config', lambda: {'cron': {'wrap_response': global_wrap}})
    job = {'id': 'wrap-job', 'name': 'Brief', 'execution_id': 'wrap-run',
           'deliver': 'telegram:555:100', 'wrap_response': job_wrap}
    queue.enqueue('wrap-run', job, 'sealed output')
    async with native.peer('telegram') as wire, native.connected('telegram', wire, monkeypatch) as (adapter, _):
        monkeypatch.setattr('plugins.platforms.telegram.adapter.TelegramAdapter.get_chat_info',
                            AsyncMock(return_value={'type': 'group'}))
        monkeypatch.setattr('gateway.config.load_gateway_config',
                            lambda: GatewayConfig(platforms={Platform.TELEGRAM: adapter.config}))
        loop = asyncio.get_running_loop()
        assert await asyncio.to_thread(scheduler.drain_delivery_queue, {Platform.TELEGRAM: adapter}, loop) == 1
        assert await asyncio.to_thread(scheduler.drain_delivery_queue, {Platform.TELEGRAM: adapter}, loop) == 0
        assert queue.get_status('wrap-run')['status'] == 'delivered'
        assert len(wire.events) == 1
        text = wire.events[0]['fields']['text']
        assert ('Cronjob Response: Brief' in text) is job_wrap
        assert ('To stop or manage this job' in text) is job_wrap
        assert 'sealed output' in text


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', ['telegram', 'slack'])
@pytest.mark.parametrize(('extra', 'origin_thread', 'target_thread', 'fresh'), [
    ({'cron_fresh_thread_per_run': True}, '100', '100', True),
    ({}, '100', '100', False),
    ({'cron_fresh_thread_per_run': False}, '100', '100', False),
    ({'cron_fresh_thread_per_run': 'yes'}, '100', '100', False),
    ({'cron_fresh_thread_per_run': True}, None, '100', False),
    ({'cron_fresh_thread_per_run': True}, '100', '101', False),
])
async def test_queued_fresh_origin_thread_is_claimed_once(
        tmp_path, monkeypatch, platform, extra, origin_thread, target_thread, fresh):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(queue, 'DELIVERY_DB', tmp_path / 'cron/deliveries.db')
    monkeypatch.setattr(scheduler, 'load_config', lambda: {'cron': {'wrap_response': False}})
    thread = lambda value: ('1700000000.' + value.zfill(6) if platform == 'slack' and value else value)
    chat = 'C12345678' if platform == 'slack' else '555'
    origin = {'platform': platform, 'chat_id': chat, 'chat_type': 'group', 'user_id': '999'}
    if origin_thread is not None:
        origin['thread_id'] = thread(origin_thread)
    if platform == 'slack':
        origin['scope_id'] = 'TLOCAL'
    job = {'id': 'topic-job', 'name': 'Brief', 'execution_id': 'topic-run',
           'deliver': f'{platform}:{chat}:{thread(target_thread)}', 'origin': origin,
           'attach_to_session': True}
    queue.enqueue('topic-run', job, 'sealed output')
    loop = asyncio.get_running_loop()

    async def exercise(adapter, events):
        monkeypatch.setattr('plugins.platforms.telegram.adapter.TelegramAdapter.get_chat_info',
                            AsyncMock(return_value={'type': 'group'}))
        adapter.config.extra.update(extra)
        config = GatewayConfig(platforms={Platform(platform): adapter.config})
        adapter._session_store = SessionStore(tmp_path / 'sessions', config)
        monkeypatch.setattr('gateway.config.load_gateway_config', lambda: config)
        adapters = {Platform(platform): adapter}
        plan = await asyncio.to_thread(strict_delivery.prepare, job, 'sealed output', False,
                                       adapters=adapters, loop=loop)
        assert events == []
        assert queue.get_status('topic-run')['status'] == 'pending'
        frozen = plan['targets'][0][3]
        if platform == 'telegram':
            assert frozen['new_thread'] is fresh
            assert frozen['plan']['binding']['thread_id'] == (None if fresh else target_thread)
        else:
            assert ('source' in frozen) is fresh
        assert await asyncio.to_thread(scheduler.drain_delivery_queue, adapters, loop) == 1
        assert queue.get_status('topic-run')['status'] == 'delivered'
        observed = len(events)
        assert await asyncio.to_thread(scheduler.drain_delivery_queue, adapters, loop) == 0
        assert len(events) == observed

    if platform == 'telegram':
        async with native.peer(platform) as wire, native.connected(platform, wire, monkeypatch) as (adapter, _):
            await exercise(adapter, wire.events)
            assert len(wire.events) == (2 if fresh else 1)
            if fresh:
                assert wire.events[0]['path'].endswith('/createForumTopic')
            assert wire.events[-1]['path'].endswith('/sendMessage')
            assert str(wire.events[-1]['fields']['message_thread_id']) == ('777' if fresh else target_thread)
    else:
        async with slack_peer.peer() as (url, events):
            config = PlatformConfig(enabled=True, token='inert-local-thread')
            adapter = SlackAdapter(config)
            client = AsyncWebClient(token=config.token, base_url=url + '/', proxy='', timeout=10)
            adapter._app = SimpleNamespace(client=client)
            adapter._team_clients = {'TLOCAL': client}
            adapter._channel_team[chat] = 'TLOCAL'
            await exercise(adapter, events)
            assert len(events) == (2 if fresh else 1)
            assert all(event['path'] == '/chat.postMessage' for event in events)
            if fresh:
                assert 'thread_ts' not in events[0]['data']
            assert events[-1]['data']['thread_ts'] == ('1700000000.000001' if fresh else thread(target_thread))
