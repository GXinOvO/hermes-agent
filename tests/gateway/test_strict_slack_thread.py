"""Whole-payload preclaim refusal and frozen native Slack creation dependencies."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cron import delivery_queue as queue, scheduler
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.session import SessionStore
from plugins.platforms.slack.adapter import SlackAdapter
from slack_sdk.web.async_client import AsyncWebClient
from tests.cron import test_strict_continuable_thread as fixture


def setup(tmp_path, monkeypatch, url):
    job = fixture.enqueue(tmp_path, monkeypatch)
    config = GatewayConfig(platforms={Platform.SLACK: PlatformConfig(enabled=True, token='inert-local-thread')})
    monkeypatch.setattr(scheduler, 'load_config', lambda: {'cron': {'wrap_response': False, 'mirror_delivery': True}})
    monkeypatch.setattr('gateway.config.load_gateway_config', lambda: config)
    adapter = SlackAdapter(config.platforms[Platform.SLACK])
    client = AsyncWebClient(token='inert-local-thread', base_url=url + '/', proxy='')
    adapter._app = SimpleNamespace(client=client)
    adapter._team_clients = {'TLOCAL': client}
    adapter._session_store = SessionStore(tmp_path / 'sessions', config)
    return job, adapter


@pytest.mark.asyncio
@pytest.mark.parametrize('defect', ['workspace', 'creator', 'store', 'missing_media', 'later_target', 'media_aba'])
async def test_full_preflight_refuses_before_root_claim(tmp_path, monkeypatch, defect):
    async with fixture.peer() as (url, events):
        job, adapter = setup(tmp_path, monkeypatch, url)
        content = 'saved output'
        if defect == 'workspace':
            job['origin']['scope_id'] = 'TUNKNOWN'
        if defect == 'creator':
            adapter.create_handoff_thread = lambda *args: 'untracked'
        if defect == 'store':
            adapter._session_store = None
        if defect == 'later_target':
            job['deliver'] = 'origin,discord:123'
        if defect == 'missing_media':
            content += '\nMEDIA: ' + str(tmp_path / 'missing.txt')
        if defect == 'media_aba':
            path = tmp_path / 'attachment.txt'
            path.write_bytes(b'A durable')
            content += '\nMEDIA: ' + str(path)
            original = Path.read_bytes

            def replaced(source):
                if source != path:
                    return original(source)
                source.write_bytes(b'B transient')
                try:
                    return original(source)
                finally:
                    source.write_bytes(b'A durable')

            monkeypatch.setattr(Path, 'read_bytes', replaced)
        with queue._transaction() as conn:
            from cron.strict_delivery import media_identity
            conn.execute('UPDATE deliveries SET job_json=?, content=?, media_json=? WHERE execution_id=?',
                         (json.dumps(job, ensure_ascii=False, sort_keys=True), content,
                          json.dumps(media_identity(content)), 'thread-exec'))
        assert await asyncio.to_thread(scheduler.drain_delivery_queue, {Platform.SLACK: adapter}, asyncio.get_running_loop()) == 0
        row = queue.get_status('thread-exec')
        assert row['status'] == 'pending' and row['owner_pid'] is None and row.get('thread_json') is None
        assert events == []


@pytest.mark.asyncio
async def test_postclaim_route_token_and_media_aba_do_not_reselect(tmp_path, monkeypatch):
    from cron.strict_delivery import prepare, deliver
    async with fixture.peer() as (url, events):
        _job, adapter = setup(tmp_path, monkeypatch, url)
        path = tmp_path / 'attachment.txt'
        path.write_bytes(b'A durable')
        content = 'saved output\nMEDIA: ' + str(path)
        with queue._transaction() as conn:
            from cron.strict_delivery import media_identity
            conn.execute('UPDATE deliveries SET content=?, media_json=? WHERE execution_id=?',
                         (content, json.dumps(media_identity(content)), 'thread-exec'))
        original_claim = queue.claim_next

        def claim(**kwargs):
            row = original_claim(**kwargs)
            adapter._team_clients.clear()
            adapter._app.client.token = 'changed-after-claim'
            adapter._app.client.base_url = 'http://127.0.0.1:1/'
            adapter._channel_team['CLOCAL'] = 'TOTHER'
            path.write_bytes(b'B transient')
            path.write_bytes(b'A durable')
            return row

        monkeypatch.setattr(queue, 'claim_next', claim)
        loop = asyncio.get_running_loop()
        assert await asyncio.to_thread(queue.drain, deliver,
            prepare=lambda j, c, f: prepare(j, c, f, adapters={Platform.SLACK: adapter}, loop=loop)) == 1
    assert queue.get_status('thread-exec')['status'] == 'delivered'
    assert events[0]['data']['channel'] == events[1]['data']['channel'] == 'CLOCAL'
    assert events[1]['data']['thread_ts'] == '1700000000.000001'
    assert all(event['authorization'] == 'Bearer inert-local-thread' for event in events if event['path'] != '/upload')
    assert bytes.fromhex(events[-2]['body']) == b'A durable'