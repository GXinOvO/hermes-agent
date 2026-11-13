import asyncio
import json

import pytest
from slack_sdk.web.async_client import AsyncWebClient
from types import SimpleNamespace
from websockets.asyncio.server import serve

from cron import delivery_queue as queue, jobs, scheduler
from gateway.config import GatewayConfig, Platform, PlatformConfig
from hermes_cli.gateway_client import GATEWAY_WS_PROTOCOL
from plugins.platforms.slack.adapter import SlackAdapter
from tests.cron.test_strict_mixed_fanout import peer
from tests.gateway.test_session_bot_retry import _admissions, _settle_quietly

pytest_plugins = ['tests.cron.test_sealed_bot_consumer']


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['normal', 'lost', 'suppressed', 'diagnostic', 'done-recover', 'claimed-recover', 'database-drift'])
async def test_bot_mixed_handoff_freezes_owner_and_stops_after_lost_ack(sealed_bot, monkeypatch, case):
    from gateway.session_controls import AuthorityConnection
    from hermes_cli import gateway_client, gateway_runtime
    from tools import bot_live_delivery as mailbox
    bot = sealed_bot
    lost_ack = case == 'lost'
    failure = case in {'suppressed', 'diagnostic'}
    if failure:
        (bot.home / 'config.yaml').write_text('display:\n  suppress_warning_notifications: '
                                             + str(case == 'suppressed').lower() + '\n')
    connection = AuthorityConnection(bot.authority, lambda *a: None, {
        'user_id': bot.connection.actor.subject, 'provider': 'local', 'profile_id': bot.authority.profile_id,
        'capabilities': ['session:create', 'session:read', 'session:submit'],
        'instance_id': bot.authority.instance_id, 'native_bootstrap': True}, operator=True)
    rpcs = []

    async def receive(ws):
        async for raw in ws:
            frame = json.loads(raw)
            rpcs.append(frame)
            answer = await connection.dispatch(frame)
            assert 'result' in answer, answer
            if lost_ack and frame['method'] == 'bot_relay.deliver':
                await ws.close()
            else:
                await ws.send(json.dumps(answer))

    async with serve(receive, '127.0.0.1', 0, subprotocols=[GATEWAY_WS_PROTOCOL]) as server, peer() as (url, events):
        port = server.sockets[0].getsockname()[1]
        endpoint = gateway_runtime.GatewayEndpoint(str(bot.home), bot.authority.instance_id, 1, 1,
            f'http://127.0.0.1:{port}', 'none', frozenset({'session-authority-v1'}))
        monkeypatch.setattr(gateway_runtime, 'discover_gateway_endpoint',
                            lambda *a, **kw: gateway_runtime.GatewayDiscovery('ready', endpoint))
        monkeypatch.setattr(gateway_client, '_session_ticket', lambda *a, **kw: 'inert-bot-ticket')
        pconfig = PlatformConfig(enabled=True, token='inert-bot-fanout')
        config = GatewayConfig(platforms={Platform.SLACK: pconfig})
        monkeypatch.setattr('gateway.config.load_gateway_config', lambda: config)
        monkeypatch.setattr(scheduler, 'load_config', lambda: {'cron': {'wrap_response': False},
            'display': {'suppress_warning_notifications': False}})
        adapter = SlackAdapter(pconfig)
        client = AsyncWebClient(token=pconfig.token, base_url=url + '/', proxy='', timeout=5)
        adapter._app = SimpleNamespace(client=client)
        adapter._team_clients = {'TLOCAL': client}
        adapter._channel_team.update({'CLOCAL': 'TLOCAL', 'C12345678': 'TLOCAL'})
        adapter._session_store = bot.authority.runner.session_store
        script = bot.home / 'scripts' / 'mixed.py'
        script.parent.mkdir(exist_ok=True)
        script.write_text('from pathlib import Path\np=Path(__file__).parents[1]/"mixed-producer-calls"\n'
                          'with p.open("a") as f: f.write("executed\\n")\nprint("EXACT_MIXED_BOT_RESULT")\n')
        job = jobs.create_job(prompt=None, schedule='every 1h', script=script.name, no_agent=True,
            deliver='slack:C12345678:1700000777.000001,bot-chat,origin',
            failure_deliver='slack:C12345678:1700000777.000001,bot-chat,origin',
            origin={'platform': 'slack', 'chat_id': 'CLOCAL', 'scope_id': 'TLOCAL', 'user_id': 'UHUMAN'})
        assert await asyncio.to_thread(scheduler._run_one_job_body, job)
        if failure:
            with queue._transaction() as conn:
                conn.execute('UPDATE deliveries SET for_failure=1')
        claim = queue.claim_next
        original_transition = __import__('cron.strict_thread_delivery', fromlist=['transition']).transition
        stopped = []

        def transition(execution, index, before, after, receipt=None):
            value = original_transition(execution, index, before, after, receipt)
            wanted = 'done' if case == 'done-recover' else 'target_claimed'
            if value and index == 1 and after == wanted and not stopped and case.endswith('-recover'):
                stopped.append(True)
                raise SystemExit('fixture death after ' + after)
            return value

        monkeypatch.setattr('cron.strict_thread_delivery.transition', transition)
        finish = queue._finish
        monkeypatch.setattr(queue, '_finish', lambda *a, **kw: False if stopped and case.endswith('-recover')
                            else finish(*a, **kw))

        def claimed(**kwargs):
            row = claim(**kwargs)
            if row is not None and case not in {'done-recover', 'claimed-recover'}:
                def rediscovery(*a, **kw):
                    raise AssertionError('no discovery after claim')
                monkeypatch.setattr(gateway_runtime, 'discover_gateway_endpoint', rediscovery)
                monkeypatch.setattr(mailbox, 'find_canonical_live_owner', rediscovery)
                if case == 'database-drift':
                    database = bot.home / 'state.db'
                    replacement = bot.home / 'replacement.db'
                    replacement.write_bytes(database.read_bytes())
                    replacement.replace(database)
            return row

        monkeypatch.setattr(queue, 'claim_next', claimed)
        loop = asyncio.get_running_loop()
        assert await asyncio.to_thread(scheduler.drain_delivery_queue, {Platform.SLACK: adapter}, loop) == 1
        await _settle_quietly(bot)
        if case in {'normal', 'diagnostic', 'done-recover'} and not stopped:
            assert await asyncio.to_thread(scheduler.drain_delivery_queue, {Platform.SLACK: adapter}, loop) in (0, 1)
            await _settle_quietly(bot)
        if case.endswith('-recover'):
            assert stopped, 'the requested failure point must be exercised'
            monkeypatch.setattr(queue, '_finish', finish)
            with queue._transaction() as conn:
                conn.execute("UPDATE deliveries SET status='delivering', owner_process_id='dead-fixture', "
                             "owner_pid=2000000000, owner_started_at=1")
            if case == 'done-recover':
                monkeypatch.setattr(gateway_runtime, 'discover_gateway_endpoint',
                                    lambda *a, **kw: (_ for _ in ()).throw(AssertionError('done Bot must not rediscover')))
        again = await asyncio.to_thread(scheduler.drain_delivery_queue, {Platform.SLACK: adapter}, loop)
        assert again == int(case == 'done-recover')
        await _settle_quietly(bot)
    admitted = case not in {'suppressed', 'claimed-recover', 'database-drift'}
    deliveries = [frame for frame in rpcs if frame['method'] == 'bot_relay.deliver']
    assert len(deliveries) == len(_admissions(bot)) == int(admitted)
    if admitted:
        assert deliveries[0]['params']['sealed_cron_result_v1']['target_session_id'] == bot.chat
        assert deliveries[0]['params']['message'].endswith('EXACT_MIXED_BOT_RESULT')
        assert deliveries[0]['params']['notification_category'] == ('diagnostic' if failure else 'result')
        assert _admissions(bot)[0]['payload']['local_automation_v1']['result_recovery_v1'] == 'consume_only'
    assert (bot.home / 'mixed-producer-calls').read_text().splitlines() == ['executed']
    with queue._transaction() as conn:
        row = dict(conn.execute('SELECT * FROM deliveries').fetchone())
    uncertain = case in {'lost', 'claimed-recover', 'database-drift'}
    assert row['status'] == ('unknown' if uncertain else 'delivered')
    assert len(events) == (1 if uncertain else 2)
    assert [event['data']['channel'] for event in events] == (['C12345678'] if uncertain else ['C12345678', 'CLOCAL'])
