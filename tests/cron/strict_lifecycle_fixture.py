import asyncio
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def produce(home):
    from cron import delivery_queue, executions, scheduler
    request = json.loads((Path(home) / 'lifecycle-request.json').read_text())
    job = request['job']
    execution = job['execution_id']
    assert executions.adopt_claimed_execution(execution)
    os.environ['_HERMES_CRON_EXTERNAL_WORKER'] = execution
    original = delivery_queue.enqueue

    def crash_after_enqueue(*args, **kwargs):
        original(*args, **kwargs)
        os._exit(73)

    with patch.object(delivery_queue, 'enqueue', crash_after_enqueue):
        scheduler._run_one_job_body(job)
    raise AssertionError('producer enqueue seam not reached')


async def consume(home, url, mode):
    from cron import delivery_queue as queue, scheduler
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.session import SessionSource, SessionStore
    from plugins.platforms.slack.adapter import SlackAdapter
    from slack_sdk.web.async_client import AsyncWebClient

    request = json.loads((Path(home) / 'lifecycle-request.json').read_text())
    execution = request['job']['execution_id']
    extra = {'reactions': False}
    if mode == 'strict_mention':
        extra['strict_mention'] = True
    pconfig = PlatformConfig(enabled=True, token='inert-local-thread', extra=extra)
    pconfig.typing_indicator = False
    config = GatewayConfig(platforms={Platform.SLACK: pconfig})
    adapter = SlackAdapter(pconfig)
    client = AsyncWebClient(token=pconfig.token, base_url=url + '/', proxy='', timeout=10)
    adapter._app = SimpleNamespace(client=client)
    adapter._team_clients = {'TLOCAL': client}
    adapter._team_bot_user_ids = {'TLOCAL': 'UBOT'}
    adapter._bot_user_id = 'UBOT'
    adapter._channel_team['CLOCAL'] = 'TLOCAL'
    adapter._user_is_bot_cache[('TLOCAL', 'UHUMAN')] = False
    adapter._user_is_bot_cache[('TLOCAL', 'UBLOCKED')] = False
    adapter._user_name_cache[('TLOCAL', 'UHUMAN')] = 'Human'
    adapter._channel_name_cache[('TLOCAL', 'CLOCAL')] = 'local-channel'
    store = adapter._session_store = SessionStore(Path(home) / 'sessions', config)
    original_create = queue.claim_thread_create
    original_body = queue.claim_thread_body

    def create_claim(*args, **kwargs):
        result = original_create(*args, **kwargs)
        if result and mode == 'create_claim':
            os._exit(88)
        return result

    def body_claim(*args, **kwargs):
        if mode == 'root_ack':
            os._exit(88)
        result = original_body(*args, **kwargs)
        if result and mode == 'body_claim':
            os._exit(88)
        return result

    with patch.object(scheduler, 'load_config', return_value={'cron': {'wrap_response': False, 'mirror_delivery': True}}), \
            patch('gateway.config.load_gateway_config', return_value=config), \
            patch.object(queue, 'claim_thread_body', body_claim), patch.object(queue, 'claim_thread_create', create_claim):
        count = await asyncio.to_thread(scheduler.drain_delivery_queue, {Platform.SLACK: adapter}, asyncio.get_running_loop())
    source = SessionSource(platform=Platform.SLACK, chat_id='CLOCAL', chat_type='group',
                           user_id='UHUMAN', thread_id='1700000000.000001', scope_id='TLOCAL')
    store._ensure_loaded()
    key = store._generate_session_key(source)
    entry = store._entries.get(key)
    seeded_id = entry.session_id if entry else None
    seeded_transcript = store.load_transcript(seeded_id) if seeded_id else []
    routes = []
    auth = []
    if mode in {'inbound', 'unauthorized', 'strict_mention', 'inactive', 'ended', 'reset', 'suspended',
                'ended_mention', 'reset_mention'}:
        from datetime import timedelta
        from gateway.run import GatewayRunner
        runner = object.__new__(GatewayRunner)
        runner.adapters = {Platform.SLACK: adapter}
        runner.config = config
        runner.pairing_store = None

        def inactive():
            entry.updated_at -= timedelta(days=90)
            store._save()

        def suspended():
            entry.suspended = True
            store._save()

        change = {'inactive': inactive, 'ended': lambda: store._db.end_session(seeded_id, 'session_reset'),
                  'ended_mention': lambda: store._db.end_session(seeded_id, 'completed'),
                  'reset': lambda: store.reset_session(key), 'reset_mention': lambda: store.reset_session(key),
                  'suspended': suspended}.get(mode)
        if change:
            change()

        def authorize(user, chat_type, chat_id, **kwargs):
            candidate = SessionSource(platform=Platform.SLACK, user_id=user, chat_id=chat_id,
                                      chat_type=chat_type, thread_id=kwargs.get('thread_id'), scope_id='TLOCAL')
            verdict = runner._is_user_authorized(candidate)
            auth.append({'user': user, 'thread': kwargs.get('thread_id'), 'allowed': verdict})
            return verdict

        async def handle(event):
            route_key = store._generate_session_key(event.source)
            target = store.get_or_create_session(event.source)
            routes.append({'key': route_key, 'session_id': target.session_id,
                           'source': event.source.to_dict(), 'text': event.text,
                           'channel_context': event.channel_context,
                           'assembled_text': runner._prefix_inbound_sender_context(event, event.source, event.text),
                           'transcript': store.load_transcript(target.session_id)})

        adapter.set_authorization_check(authorize)
        adapter.set_message_handler(handle)
        user = 'UBLOCKED' if mode == 'unauthorized' else 'UHUMAN'
        explicit = mode in {'suspended', 'ended_mention', 'reset_mention'}
        text = '<@UBOT> continue explicitly' if explicit else 'continue saved brief'
        await adapter._handle_slack_message({'type': 'message', 'channel': 'CLOCAL', 'channel_type': 'channel',
            'user': user, 'team': 'TLOCAL', 'ts': '1700000001.000002', 'thread_ts': '1700000000.000001',
            'client_msg_id': 'inert-human-reply', 'text': text}, {'team_id': 'TLOCAL'})
        tasks = list(adapter._background_tasks)
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks), 20)
    script_calls = Path(home) / 'script-calls'
    print(json.dumps({'processed': count, 'row': queue.get_status(execution), 'pid': os.getpid(),
        'seeded_id': seeded_id, 'seeded_transcript': seeded_transcript, 'routes': routes, 'auth': auth,
        'script_calls': script_calls.read_text().splitlines() if script_calls.exists() else []}), flush=True)


if __name__ == '__main__':
    if sys.argv[2] == 'produce':
        produce(sys.argv[1])
    else:
        asyncio.run(consume(*sys.argv[1:]))
