"""Fresh native Slack queue consumer; only inert config/SDK endpoint is supplied."""
import asyncio
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


async def consume(home, url, stop):
    from cron import delivery_queue as queue, scheduler
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.session import SessionStore
    from plugins.platforms.slack.adapter import SlackAdapter
    from slack_sdk.web.async_client import AsyncWebClient

    pconfig = PlatformConfig(enabled=True, token='inert-local-thread')
    config = GatewayConfig(platforms={Platform.SLACK: pconfig})
    adapter = SlackAdapter(pconfig)
    client = AsyncWebClient(token=pconfig.token, base_url=url + '/', proxy='', timeout=10)
    adapter._app = SimpleNamespace(client=client)
    adapter._team_clients = {'TLOCAL': client}
    adapter._session_store = SessionStore(Path(home) / 'sessions', config)
    original = queue.claim_next

    def claim(**kwargs):
        if stop == 'before_claim':
            os._exit(88)
        if stop == 'race':
            import urllib.request
            with urllib.request.urlopen(url + '/claim-gate', timeout=20) as response:
                assert response.read() == b'go'
        row = original(**kwargs)
        return row

    def create_claim(*args, **kwargs):
        result = original_create(*args, **kwargs)
        if result and stop == 'create_claim':
            os._exit(88)
        return result

    def body_claim(*args, **kwargs):
        if stop == 'root_ack':
            os._exit(88)
        result = original_body(*args, **kwargs)
        if result and stop == 'body_claim':
            os._exit(88)
        return result

    with patch.object(scheduler, 'load_config', return_value={'cron': {'wrap_response': False, 'mirror_delivery': True}}), \
            patch('gateway.config.load_gateway_config', return_value=config), patch.object(queue, 'claim_next', claim):
        if hasattr(queue, 'claim_thread_body'):
            original_body = queue.claim_thread_body
            original_create = queue.claim_thread_create
            with patch.object(queue, 'claim_thread_body', body_claim), patch.object(queue, 'claim_thread_create', create_claim):
                count = await asyncio.to_thread(scheduler.drain_delivery_queue, {Platform.SLACK: adapter}, asyncio.get_running_loop())
        else:
            count = await asyncio.to_thread(scheduler.drain_delivery_queue, {Platform.SLACK: adapter}, asyncio.get_running_loop())
    from gateway.session import SessionSource
    source = SessionSource(platform=Platform.SLACK, chat_id='CLOCAL', chat_type='group',
                           user_id='UHUMAN', thread_id='1700000000.000001', scope_id='TLOCAL')
    store = adapter._session_store
    store._ensure_loaded()
    key = store._generate_session_key(source)
    entry = store._entries.get(key)
    if entry is not None:
        original_id = entry.session_id
        entry = store.get_or_create_session(source)
        assert entry.session_id == original_id
    transcript = store.load_transcript(entry.session_id) if entry else []
    runner = Path(home) / 'runner-count'
    print(json.dumps({'processed': count, 'row': queue.get_status('thread-exec'), 'pid': os.getpid(),
                      'key': key, 'session_id': entry.session_id if entry else None, 'transcript': transcript,
                      'runner_count': runner.read_text() if runner.exists() else '0'}), flush=True)


if __name__ == '__main__':
    asyncio.run(consume(*sys.argv[1:]))