import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace

from slack_sdk.web.async_client import AsyncWebClient
from websockets.asyncio.server import serve

from cron import delivery_queue as queue, jobs, scheduler
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.session_controls import AuthorityConnection
from hermes_cli.gateway_client import GATEWAY_WS_PROTOCOL
from plugins.platforms.slack.adapter import SlackAdapter
from tests.cron.test_strict_mixed_fanout import peer
from tests.gateway.test_session_bot_retry import _settle_quietly


@asynccontextmanager
async def cached_fanout(bot, monkeypatch, *, diagnostic=False):
    from hermes_cli import gateway_client, gateway_runtime
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
            await ws.send(json.dumps(answer))

    async with serve(receive, '127.0.0.1', 0, subprotocols=[GATEWAY_WS_PROTOCOL]) as server, peer() as (url, events):
        port = server.sockets[0].getsockname()[1]
        endpoint = gateway_runtime.GatewayEndpoint(str(bot.home), bot.authority.instance_id, 1, 1,
            f'http://127.0.0.1:{port}', 'none', frozenset({'session-authority-v1'}))
        monkeypatch.setattr(gateway_runtime, 'discover_gateway_endpoint',
                            lambda *a, **kw: gateway_runtime.GatewayDiscovery('ready', endpoint))
        monkeypatch.setattr(gateway_client, '_session_ticket', lambda *a, **kw: 'inert-cache-ticket')
        pconfig = PlatformConfig(enabled=True, token='inert-cache-token')
        monkeypatch.setattr('gateway.config.load_gateway_config', lambda: GatewayConfig(platforms={Platform.SLACK: pconfig}))
        monkeypatch.setattr(scheduler, 'load_config', lambda: {'cron': {'wrap_response': False}})
        adapter = SlackAdapter(pconfig)
        client = AsyncWebClient(token=pconfig.token, base_url=url + '/', proxy='', timeout=5)
        adapter._app = SimpleNamespace(client=client)
        adapter._team_clients = {'TLOCAL': client}
        adapter._channel_team.update({'CLOCAL': 'TLOCAL', 'C12345678': 'TLOCAL'})
        adapter._session_store = bot.authority.runner.session_store
        script = bot.home / 'scripts' / 'cache-producer.py'
        script.parent.mkdir(exist_ok=True)
        script.write_text('from pathlib import Path\np=Path(__file__).parents[1]/"cache-producer-count"\n'
                          'with p.open("a") as f: f.write("executed\\n")\nprint("CACHE_SEALED_BODY")\n')
        job = jobs.create_job(prompt=None, schedule='every 1h', script=script.name, no_agent=True,
            deliver='slack:C12345678:1700000777.000001,bot-chat,origin',
            failure_deliver='slack:C12345678:1700000777.000001,bot-chat,origin',
            origin={'platform': 'slack', 'chat_id': 'CLOCAL', 'scope_id': 'TLOCAL', 'user_id': 'UHUMAN'})
        assert await asyncio.to_thread(scheduler._run_one_job_body, job)
        with queue._transaction() as conn:
            if diagnostic:
                conn.execute('UPDATE deliveries SET for_failure=1')
            row = dict(conn.execute('SELECT * FROM deliveries').fetchone())
        from cron.strict_bot_delivery import freeze
        target = {'platform': 'bot-chat', 'chat_id': '', 'thread_id': None}
        _, identity = await asyncio.to_thread(freeze, target, json.loads(row['job_json']), row['content'], diagnostic)
        path = bot.home / 'runtime' / 'bot_live_delivery' / (identity['params']['id'] + '.json')
        yield SimpleNamespace(bot=bot, connection=connection, adapter=adapter, events=events, rpcs=rpcs,
                              row=row, identity=identity, path=path, target=target, diagnostic=diagnostic)
        await _settle_quietly(bot)


async def admit_cached(case):
    response = await case.connection.dispatch({'id': 10, 'method': 'bot_relay.deliver', 'params': case.identity['params']})
    assert 'result' in response, response
    await _settle_quietly(case.bot)
    return json.loads(case.path.read_text())


async def drain_cached(case):
    loop = asyncio.get_running_loop()
    adapters = {Platform.SLACK: case.adapter}
    processed = await asyncio.to_thread(scheduler.drain_delivery_queue, adapters, loop)
    await _settle_quietly(case.bot)
    again = await asyncio.to_thread(scheduler.drain_delivery_queue, adapters, loop)
    with queue._transaction() as conn:
        final = dict(conn.execute('SELECT * FROM deliveries WHERE execution_id=?', (case.row['execution_id'],)).fetchone())
    assert (case.bot.home / 'cache-producer-count').read_text().splitlines() == ['executed']
    assert again == 0
    return processed, final


def mutate_native(bot, receipt, case):
    def corrupt(conn):
        row = dict(conn.execute('SELECT * FROM session_admissions WHERE admission_id=?', (receipt['admission_id'],)).fetchone())
        payload = json.loads(row['payload_json'])
        descriptor = payload['local_automation_v1']
        container, key, value = {
            'db_provenance': (descriptor['sealed_cron_result_v1'], 'seal', '0' * 64),
            'db_policy': (descriptor, 'result_recovery_v1', 'retry'),
        }[case]
        container[key] = value
        conn.execute('UPDATE session_admissions SET payload_json=? WHERE admission_id=?', (json.dumps(payload), receipt['admission_id']))
    bot.authority.db._execute_write(corrupt)
