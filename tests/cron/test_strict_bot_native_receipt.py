"""Independent W1 production-path counterexamples, inert loopback only."""
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
import pytest
from slack_sdk.web.async_client import AsyncWebClient
from websockets.asyncio.server import serve
from cron import delivery_queue as queue, jobs, scheduler
from cron.sealed_bot_result import delivery_identity
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.session_controls import AuthorityConnection
from hermes_cli.gateway_client import GATEWAY_WS_PROTOCOL
from plugins.platforms.slack.adapter import SlackAdapter
from tests.cron.test_strict_mixed_fanout import peer
from tests.gateway.test_session_bot_retry import _admissions, _settle_quietly

pytest_plugins = ['tests.cron.test_sealed_bot_consumer']


def admissions(bot):
    return [row for sid in bot.authority.sessions for row in
            _admissions(SimpleNamespace(authority=bot.authority, chat=sid))]


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['control', 'new_bot', 'malformed_receipt', 'category_receipt',
                                  'principal_receipt', 'author_receipt', 'sealed_receipt', 'status_receipt',
                                  'forged_without_admission', 'nonexistent_admission',
                                  'db_category', 'db_principal', 'db_message', 'valid_cached_receipt'])
async def test_public_mixed_fanout_boundary(sealed_bot, monkeypatch, case):
    from hermes_cli import gateway_client, gateway_runtime
    bot = sealed_bot
    monkeypatch.setattr('gateway.run._load_gateway_config', lambda *a, **kw: {'platform_toolsets': {'cli': []}, 'model': {'default': 'fixture'}})
    if case == 'new_bot':
        assert bot.authority.db.set_session_title(bot.chat, 'Previous Chat')
        assert bot.authority.db.get_session_by_title('Bot Chat') is None
    connection = AuthorityConnection(bot.authority, lambda *a: None, {
        'user_id': bot.connection.actor.subject, 'provider': 'local', 'profile_id': bot.authority.profile_id,
        'capabilities': ['session:create', 'session:read', 'session:submit'],
        'instance_id': bot.authority.instance_id, 'native_bootstrap': True}, operator=True)
    rpcs, answers = [], []

    async def receive(ws):
        async for raw in ws:
            frame = json.loads(raw)
            rpcs.append(frame)
            answer = await connection.dispatch(frame)
            answers.append(answer)
            await ws.send(json.dumps(answer))

    async with serve(receive, '127.0.0.1', 0, subprotocols=[GATEWAY_WS_PROTOCOL]) as server, peer() as (url, events):
        port = server.sockets[0].getsockname()[1]
        endpoint = gateway_runtime.GatewayEndpoint(str(bot.home), bot.authority.instance_id, 1, 1,
            f'http://127.0.0.1:{port}', 'none', frozenset({'session-authority-v1'}))
        monkeypatch.setattr(gateway_runtime, 'discover_gateway_endpoint',
                            lambda *a, **kw: gateway_runtime.GatewayDiscovery('ready', endpoint))
        monkeypatch.setattr(gateway_client, '_session_ticket', lambda *a, **kw: 'inert-review-ticket')
        pconfig = PlatformConfig(enabled=True, token='inert-review-token')
        config = GatewayConfig(platforms={Platform.SLACK: pconfig})
        monkeypatch.setattr('gateway.config.load_gateway_config', lambda: config)
        monkeypatch.setattr(scheduler, 'load_config', lambda: {'cron': {'wrap_response': False}})
        adapter = SlackAdapter(pconfig)
        client = AsyncWebClient(token=pconfig.token, base_url=url + '/', proxy='', timeout=5)
        adapter._app = SimpleNamespace(client=client)
        adapter._team_clients = {'TLOCAL': client}
        adapter._channel_team.update({'CLOCAL': 'TLOCAL', 'C12345678': 'TLOCAL'})
        adapter._session_store = bot.authority.runner.session_store
        script = bot.home / 'scripts' / 'review-producer.py'
        script.parent.mkdir(exist_ok=True)
        script.write_text('from pathlib import Path\np=Path(__file__).parents[1]/"review-producer-count"\n'
                          'with p.open("a") as f: f.write("executed\\n")\nprint("REVIEW_SEALED_BODY")\n')
        job = jobs.create_job(prompt=None, schedule='every 1h', script=script.name, no_agent=True,
            deliver='slack:C12345678:1700000777.000001,bot-chat,origin',
            origin={'platform': 'slack', 'chat_id': 'CLOCAL', 'scope_id': 'TLOCAL', 'user_id': 'UHUMAN'})
        assert await asyncio.to_thread(scheduler._run_one_job_body, job)
        with queue._transaction() as conn:
            row = dict(conn.execute('SELECT * FROM deliveries').fetchone())
        if case.endswith('_receipt') or case in {'forged_without_admission', 'nonexistent_admission',
                                                'db_category', 'db_principal', 'db_message'}:
            from cron.strict_bot_delivery import freeze
            _, identity = await asyncio.to_thread(freeze,
                {'platform': 'bot-chat', 'chat_id': '', 'thread_id': None},
                json.loads(row['job_json']), row['content'], False)
            key = identity['params']['id']
            receipt_path = bot.home / 'runtime' / 'bot_live_delivery' / f'{key}.json'
            if case == 'forged_without_admission':
                # A corrupted/foreign writer can copy the real sealed plan but cannot create
                # a committed native admission just by writing an admission id to the mailbox.
                receipt_path.parent.mkdir(parents=True, exist_ok=True)
                receipt = dict(status='queued', delivery_id=key, profile_home=str(bot.home),
                    session_id=identity['params']['session_id'], principal_id=connection.actor.subject,
                    admission_id='f' * 32, message=identity['params']['message'],
                    sealed_cron_result_v1=identity['params']['sealed_cron_result_v1'])
                assert not admissions(bot)
            else:
                answer = await connection.dispatch({'id': 10, 'method': 'bot_relay.deliver', 'params': identity['params']})
                assert 'result' in answer, answer
                await _settle_quietly(bot)
                receipt = json.loads(receipt_path.read_text())
            if case == 'nonexistent_admission':
                receipt['admission_id'] = 'f' * 32
            elif case.startswith('db_'):
                with bot.authority.db._read_ctx() as conn:
                    admission = dict(conn.execute('SELECT * FROM session_admissions WHERE admission_id=?',
                                                  (receipt['admission_id'],)).fetchone())
                payload = json.loads(admission['payload_json'])
                if case == 'db_category':
                    payload.setdefault('local_automation_v1', {})['notification_category'] = 'diagnostic'
                elif case == 'db_message':
                    payload['text'] = 'different committed native input'
                def corrupt(conn):
                    if case == 'db_principal':
                        conn.execute('UPDATE session_admissions SET principal_id=? WHERE admission_id=?',
                                     ('uid:987654', receipt['admission_id']))
                    else:
                        conn.execute('UPDATE session_admissions SET payload_json=? WHERE admission_id=?',
                                     (json.dumps(payload), receipt['admission_id']))
                bot.authority.db._execute_write(corrupt)
            path = receipt_path
            mutations = {
                'malformed_receipt': lambda: receipt.pop('admission_id'),
                'category_receipt': lambda: receipt.update(notification_category='diagnostic'),
                'principal_receipt': lambda: receipt.update(principal_id='uid:foreign'),
                'author_receipt': lambda: receipt.update(author={'id': 'foreign', 'name': 'Foreign'}),
                'sealed_receipt': lambda: receipt['sealed_cron_result_v1'].update(seal='0' * 64),
                'status_receipt': lambda: receipt.update(status='invented'),
            }
            if case in mutations:
                mutations[case]()
            with bot.authority.db._read_ctx() as conn:
                committed = conn.execute('SELECT * FROM session_admissions WHERE admission_id=?',
                                        (receipt.get('admission_id'),)).fetchone()
            print('COMMITTED_NATIVE ' + json.dumps({'case': case,
                'mailbox_admission_id': receipt.get('admission_id'),
                'native': dict(committed) if committed is not None else None}), flush=True)
            path.write_text(json.dumps(receipt))
        processed = await asyncio.to_thread(scheduler.drain_delivery_queue, {Platform.SLACK: adapter}, asyncio.get_running_loop())
        await _settle_quietly(bot)
        again = await asyncio.to_thread(scheduler.drain_delivery_queue, {Platform.SLACK: adapter}, asyncio.get_running_loop())
        with queue._transaction() as conn:
            final = dict(conn.execute('SELECT * FROM deliveries').fetchone())
        observed = {'case': case, 'processed': processed, 'second_processed': again, 'status': final['status'],
            'wire_count': len(events), 'wire_channels': [e['data'].get('channel') for e in events],
            'rpc_count': len(rpcs), 'answers': answers, 'admissions': len(admissions(bot)),
            'producer_count': (bot.home / 'review-producer-count').read_text().splitlines(),
            'thread_receipts': final['thread_receipts_json']}
        print('W1_COUNTEREXAMPLE ' + json.dumps(observed, ensure_ascii=False), flush=True)
        assert observed['producer_count'] == ['executed']
        if case in {'control', 'new_bot', 'valid_cached_receipt'}:
            assert final['status'] == 'delivered', observed
            assert len(admissions(bot)) == 1
            assert sum(frame['method'] == 'bot_relay.deliver' for frame in rpcs) == int(case != 'valid_cached_receipt')
            if case == 'new_bot':
                params = next(frame['params'] for frame in rpcs if frame['method'] == 'bot_relay.deliver')
                assert params['session_id'] and params['session_id'] != bot.chat
                assert params['session_id'] == params['sealed_cron_result_v1']['target_session_id']
                assert params['session_id'] == params['sealed_cron_result_v1']['target_live_session_id']
            assert len(events) == 2
        else:
            assert processed == 0 and final['status'] in {'pending', 'unknown'} and not events, observed


@pytest.mark.asyncio
async def test_empty_bot_native_ordinary_door(sealed_bot, monkeypatch):
    bot = sealed_bot
    monkeypatch.setattr('gateway.run._load_gateway_config', lambda *a, **kw: {'platform_toolsets': {'cli': []}, 'model': {'default': 'fixture'}})
    assert bot.authority.db.set_session_title(bot.chat, 'Previous Chat')
    connection = AuthorityConnection(bot.authority, lambda *a: None, {
        'user_id': bot.connection.actor.subject, 'provider': 'local', 'profile_id': bot.authority.profile_id,
        'capabilities': ['session:create', 'session:read', 'session:submit'],
        'instance_id': bot.authority.instance_id, 'native_bootstrap': True}, operator=True)
    response = await connection.dispatch({'id': 20, 'method': 'bot_relay.deliver', 'params': {
        'id': 'a' * 64, 'profile': 'default', 'message': 'ordinary user input'}})
    await _settle_quietly(bot)
    print('W1_ORDINARY_EMPTY_BOT ' + json.dumps({'response': response, 'admissions': len(admissions(bot))}), flush=True)
    assert 'result' in response and len(admissions(bot)) == 1, response
