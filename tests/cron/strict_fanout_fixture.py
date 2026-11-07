"""Fresh consumer for mixed fanout; native SDKs only, inert loopback peers."""
import asyncio
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


async def consume(home, url, stop):
    from cron import delivery_queue as queue, scheduler, scheduler_delivery as delivery, strict_thread_delivery as stages
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.session import SessionStore
    from plugins.platforms.slack.adapter import SlackAdapter
    from slack_sdk.web.async_client import AsyncWebClient
    settings = json.loads((Path(home) / 'fanout-settings.json').read_text())
    pconfig = PlatformConfig(enabled=True, token='inert-fanout', extra=settings.get('extra', {}))
    config = GatewayConfig(platforms={Platform.SLACK: pconfig,
        Platform.TELEGRAM: PlatformConfig(enabled=True, token='123:inert-fanout')})
    if settings.get('cold_slack'):
        from gateway.profile_routing import ProfileRoute
        config.profile_routes = [ProfileRoute('cold-slack', 'slack', 'cold', chat_id='CCOLD')]
    adapter = SlackAdapter(pconfig)
    client = AsyncWebClient(token=pconfig.token, base_url=url + '/', proxy='', timeout=10)
    adapter._app = SimpleNamespace(client=client)
    adapter._team_clients = {'TLOCAL': client}
    adapter._channel_team.update({name: 'TLOCAL' for name in ('CLOCAL', 'C12345678', 'CSECOND', 'DFLAT')})
    adapter._session_store = SessionStore(Path(home) / 'sessions', config)
    original_transition = stages.transition
    original_claim = queue.claim_next
    original_target = delivery._prepare_target_delivery
    original_send = stages.send_target
    original_gate = None
    serialized = []
    rotation = None
    adapters = {Platform.SLACK: adapter}
    if settings.get('local'):
        from gateway.run import GatewayRunner
        from gateway.session_authority import SessionAuthority, initialize_session_authority
        from gateway.session_contract import SessionActor
        from gateway.session_local import create_local_session
        from hermes_state import AsyncSessionDB, SessionDB
        runner = GatewayRunner(GatewayConfig(sessions_dir=Path(home) / 'local-sessions'))
        database = SessionDB(Path(home) / 'state.db')
        runner.session_store._db = database
        runner._session_db = AsyncSessionDB(database)
        with patch.object(SessionAuthority, '_schedule', lambda *args: None):
            authority = await initialize_session_authority(runner, profile_id='default', instance_id='fanout', db=database)
        local_id = Path(home) / 'fanout-local-id'
        if not local_id.exists():
            actor = SessionActor('fanout-owner', 'default', frozenset({'session:create', 'session:read', 'session:submit'}), 'fixture')
            ref = create_local_session(authority, actor, dict(request_id='fanout-local', source='cli', model='fixture', toolsets=[]))
            local_id.write_text(ref.session_id)
        else:
            from gateway.session_local_recovery import restore_local_session
            restore_local_session(authority, local_id.read_text())
        from gateway.session_cron import bind_owner
        bind_owner(authority)
        local_live = authority.sessions[local_id.read_text()]
        original_local = (authority.profile_id, authority.epoch, local_live.route, local_live.source.user_name)
        row = queue.get_status('fanout-exec')
        if row['status'] == 'pending' and 'LOCAL_TARGET' in row['job_json']:
            with queue._transaction() as conn:
                conn.execute('UPDATE deliveries SET job_json=? WHERE execution_id=?',
                    (row['job_json'].replace('LOCAL_TARGET', local_id.read_text()), 'fanout-exec'))
    if settings.get('native_telegram'):
        from telegram import Bot
        from plugins.platforms.telegram.adapter import TelegramAdapter
        telegram = TelegramAdapter(config.platforms[Platform.TELEGRAM])
        telegram._bot = Bot('123:inert-fanout', base_url=url + '/bot')
        telegram._rich_mode = 'off'
        adapters[Platform.TELEGRAM] = telegram

    def drift():
        live = authority.sessions[local_id.read_text()]
        changes = {
            'source': lambda: setattr(live.source, 'user_name', 'changed-after-claim'),
            'route': lambda: setattr(live, 'route', live.route + ':changed'),
            'profile': lambda: setattr(authority, 'profile_id', 'different-profile'),
            'epoch': lambda: setattr(authority, 'epoch', authority.epoch + 1),
        }
        changes[settings['drift']]()

    def send_target(state, frozen):
        if frozen['kind'] == 'local' and settings.get('drift_at') == 'claimed':
            drift()
        return original_send(state, frozen)

    def gate(*args, **kwargs):
        guard = original_gate(*args, **kwargs)

        def at_commit(conn):
            if settings.get('drift_at') == 'commit':
                drift()
            guard(conn)
            if settings.get('drift_at') == 'serialize' and rotation is not None and not serialized:
                write_ready.set()
                assert rotation_attempted.wait(10)
                serialized.append(not rotated.wait(0.05))
        return at_commit

    def target(job, route, **kwargs):
        if route['chat_id'] == 'DFLAT':
            import copy
            flat = GatewayConfig(platforms={Platform.SLACK: PlatformConfig(enabled=True, token='inert-fanout',
                extra={'cron_continuable_surface': 'in_channel'})})
            kwargs['config'] = flat
            flat_adapter = copy.copy(adapter)
            flat_adapter.config = flat.platforms[Platform.SLACK]
            kwargs['adapters'] = {Platform.SLACK: flat_adapter}
        if route['chat_id'] == 'CCOLD':
            kwargs['adapters'] = {}
            kwargs['config'] = GatewayConfig(platforms={Platform.SLACK:
                PlatformConfig(enabled=True, token='inert-cold-fanout')})
        state = original_target(job, route, **kwargs)
        if route['platform'] == 'telegram' and state is not None:
            state.mirror_this_target = False
        return state

    def transition(execution, index, before, after, receipt=None):
        if stop == f'before:{index}:{after}':
            os._exit(88)
        value = original_transition(execution, index, before, after, receipt)
        if value and stop == f'after:{index}:{after}':
            os._exit(88)
        return value

    def claim(**kwargs):
        if stop == 'race':
            import urllib.request
            with urllib.request.urlopen(url + '/claim-gate', timeout=20) as response:
                assert response.read() == b'go'
        value = original_claim(**kwargs)
        if value and stop == 'mutate':
            adapter._team_clients.clear()
            client.token, client.base_url = 'changed-after-claim', 'http://127.0.0.1:1/'
            for media in json.loads(value['media_json'] or '[]'):
                Path(media[0]).write_bytes(b'B after claim')
        if value and stop == 'after_claim':
            os._exit(88)
        return value

    with ExitStack() as stack:
        stack.enter_context(patch.object(scheduler, 'load_config', return_value={
            'cron': {'wrap_response': False, 'mirror_delivery': True}}))
        stack.enter_context(patch('gateway.config.load_gateway_config', return_value=config))
        stack.enter_context(patch.object(stages, 'transition', transition))
        stack.enter_context(patch.object(stages, 'send_target', send_target))
        stack.enter_context(patch.object(delivery, '_prepare_target_delivery', target))
        stack.enter_context(patch.object(queue, 'claim_next', claim))
        if settings.get('cold_telegram'):
            stack.enter_context(patch('tools.send_message_strict.TELEGRAM_API', url))
        if settings.get('cold_slack'):
            import tools.send_message_strict_slack as standalone
            original_prepare = standalone.prepare

            def cold_prepare(*args, **kwargs):
                from dataclasses import replace
                plan = original_prepare(*args, **kwargs)
                options = dict(plan.client_options)
                options.update(base_url=url + '/', proxy='')
                return replace(plan, client_options=tuple(options.items()))

            stack.enter_context(patch.object(standalone, 'prepare', cold_prepare))
        if settings.get('local'):
            stack.enter_context(patch.object(SessionAuthority, '_schedule', lambda *args: None))
            if settings.get('drift_at'):
                original_gate = authority._admission_gate
                stack.enter_context(patch.object(authority, '_admission_gate', gate))
            if settings.get('drift_at') == 'serialize' and row['status'] in {'pending', 'delivering'}:
                from threading import Event
                write_ready, rotation_attempted, rotated = Event(), Event(), Event()

                async def rotate():
                    assert await asyncio.to_thread(write_ready.wait, 15)
                    rotation_attempted.set()
                    async with local_live.mutation_lock:
                        local_live.source.user_name = 'authorized-concurrent-rotation'
                        rotated.set()

                rotation = asyncio.create_task(rotate())
        count = await asyncio.to_thread(scheduler.drain_delivery_queue,
            adapters, asyncio.get_running_loop())
        if rotation is not None:
            await asyncio.wait_for(rotation, 20)
        if settings.get('local'):
            from gateway.session_contract import SessionRef
            from hermes_state_runtime import list_session_admissions
            local_admissions = list_session_admissions(database, session_id=local_id.read_text(), pending_only=False)
            authority.profile_id, authority.epoch, local_live.route, local_live.source.user_name = original_local
            await authority._drain(SessionRef('default', local_id.read_text()))
    adapter._session_store._ensure_loaded()
    print(json.dumps({'processed': count, 'pid': os.getpid(), 'row': queue.get_status('fanout-exec'),
        'local_messages': database.get_messages(local_id.read_text()) if settings.get('local') else [],
        'local_admissions': local_admissions if settings.get('local') else [],
        'serialized': serialized,
        'sessions': {key: {'id': entry.session_id, 'transcript': adapter._session_store.load_transcript(entry.session_id)}
                     for key, entry in adapter._session_store._entries.items()}}), flush=True)
    if settings.get('local'):
        database.close()


if __name__ == '__main__':
    asyncio.run(consume(*sys.argv[1:]))
