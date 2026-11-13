import asyncio
import json
import os
from pathlib import Path
import sys

import pytest
from websockets.asyncio.server import serve

from cron import delivery_queue as queue, jobs, scheduler
from gateway.session_controls import AuthorityConnection
from hermes_cli.gateway_client import GATEWAY_WS_PROTOCOL
from tests.cron.test_strict_mixed_fanout import peer
from tests.gateway.test_session_bot_retry import _admissions, _settle_quietly

pytest_plugins = ['tests.cron.test_sealed_bot_consumer']


async def consumer(home, slack_url, bot_url, stop='none', *, allow_pending=False):
    scratch = home / 'consumer-tmp'
    scratch.mkdir(exist_ok=True)
    env = {key: value for key, value in os.environ.items() if key in {'PATH', 'TZ', 'LANG'}}
    env.update(HOME=str(home), USERPROFILE=str(home), HERMES_HOME=str(home), TMPDIR=str(scratch))
    process = await asyncio.create_subprocess_exec(sys.executable,
        str(Path(__file__).with_name('strict_bot_fanout_fixture.py')), str(home), slack_url, bot_url, stop,
        env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await asyncio.wait_for(process.communicate(), 60)
    print('BOT_FANOUT_OWNER ' + json.dumps({'pid': process.pid, 'exit': process.returncode,
          'stdout': stdout.decode(), 'stderr': stderr.decode()}))
    if stop != 'none' and process.returncode == 0 and allow_pending:
        row = json.loads(stdout)
        assert row['row']['status'] == 'pending', row
        return row
    assert process.returncode == (0 if stop == 'none' else 88), stderr.decode()
    return json.loads(stdout) if stdout else None


def produce(home):
    script = home / 'scripts' / 'mixed-process.py'
    script.parent.mkdir(exist_ok=True)
    script.write_text('from pathlib import Path\np=Path(__file__).parents[1]/"restart-producer-calls"\n'
                      'with p.open("a") as f: f.write("executed\\n")\nprint("SEALED_PROCESS_RESULT")\n')
    job = jobs.create_job(prompt=None, schedule='every 1h', script=script.name, no_agent=True,
        deliver='slack:C12345678:1700000777.000001,bot-chat,origin',
        origin={'platform': 'slack', 'chat_id': 'CLOCAL', 'scope_id': 'TLOCAL', 'user_id': 'UHUMAN'})
    assert scheduler._run_one_job_body(job)


@pytest.mark.asyncio
@pytest.mark.parametrize('stop,status,admitted,wire_count', [
    ('after_claim', 'delivered', 1, 2),
    ('after:0:done', 'delivered', 1, 2),
    ('after:1:target_claimed', 'unknown', 0, 1),
    ('before:1:bot_waiting', 'unknown', 1, 1),
    ('after:1:bot_waiting', 'delivered', 1, 2),
    ('before:1:done', 'unknown', 1, 1),
    ('after:1:done', 'delivered', 1, 2),
    ('after:2:done', 'delivered', 1, 2)])
async def test_fresh_bot_fanout_consumer_never_reexecutes_sealed_producer(
        sealed_bot, monkeypatch, stop, status, admitted, wire_count):
    bot = sealed_bot
    connection = AuthorityConnection(bot.authority, lambda *a: None, {
        'user_id': bot.connection.actor.subject, 'provider': 'local', 'profile_id': bot.authority.profile_id,
        'instance_id': bot.authority.instance_id, 'native_bootstrap': True,
        'capabilities': ['session:create', 'session:read', 'session:submit']}, operator=True)
    requests = []

    async def receive(ws):
        async for raw in ws:
            frame = json.loads(raw)
            requests.append(frame)
            answer = await connection.dispatch(frame)
            assert 'result' in answer, answer
            await ws.send(json.dumps(answer))

    monkeypatch.setattr(scheduler, 'load_config', lambda: {'cron': {'wrap_response': False}})
    release = None
    if stop in {'before:1:bot_waiting', 'after:1:bot_waiting'}:
        from gateway import session_finite
        original = session_finite.execute_finite_admission
        release = asyncio.Event()

        async def held(authority, ref, row):
            await release.wait()
            return await original(authority, ref, row)

        monkeypatch.setattr(session_finite, 'execute_finite_admission', held)
    await asyncio.to_thread(produce, bot.home)
    async with serve(receive, '127.0.0.1', 0, subprotocols=[GATEWAY_WS_PROTOCOL]) as server, peer() as (url, events):
        port = server.sockets[0].getsockname()[1]
        bot_url = f'http://127.0.0.1:{port}'
        try:
            paused = await consumer(bot.home, url, bot_url, stop,
                allow_pending=stop in {'before:1:done', 'after:1:done', 'after:2:done'})
        finally:
            if release is not None:
                release.set()
        if stop in {'before:1:bot_waiting', 'after:1:bot_waiting'}:
            with queue._transaction() as conn:
                crashed = dict(conn.execute('SELECT * FROM deliveries').fetchone())
            assert crashed['status'] == 'delivering'
            assert json.loads(crashed['thread_json'])['items'][1]['stage'] == (
                'target_claimed' if stop.startswith('before') else 'bot_waiting')
        if paused is not None:
            if stop == 'before:1:done':
                waiting = json.loads(paused['row']['thread_json'])['items'][1]
                assert waiting['stage'] == 'bot_waiting' and waiting['receipt']['admission_id']
                status, wire_count = 'delivered', 2
            await _settle_quietly(bot)
            assert await consumer(bot.home, url, bot_url, stop) is None
            if stop == 'before:1:done':
                with queue._transaction() as conn:
                    crashed = dict(conn.execute('SELECT * FROM deliveries').fetchone())
                assert crashed['status'] == 'delivering'
                assert json.loads(crashed['thread_json'])['items'][1]['stage'] == 'bot_waiting'
        await _settle_quietly(bot)
        first = await consumer(bot.home, url, bot_url)
        await _settle_quietly(bot)
        if first['row']['status'] == 'pending':
            first = await consumer(bot.home, url, bot_url)
            await _settle_quietly(bot)
        assert first['row']['status'] == status
        snapshot = list(events)
        second = await consumer(bot.home, url, bot_url)
        assert events == snapshot
        await _settle_quietly(bot)
    assert first['row']['status'] == second['row']['status'] == status
    deliveries = [frame for frame in requests if frame['method'] == 'bot_relay.deliver']
    assert len(deliveries) == len(_admissions(bot)) == admitted
    assert len(events) == wire_count
    if admitted:
        assert deliveries[0]['params']['sealed_cron_result_v1']['target_session_id'] == bot.chat
        assert _admissions(bot)[0]['payload']['local_automation_v1']['result_recovery_v1'] == 'consume_only'
    assert (bot.home / 'restart-producer-calls').read_text().splitlines() == ['executed']
    audit = json.loads(second['row']['thread_receipts_json'])
    assert [item['kind'] for item in audit['items']] == ['slack', 'bot-chat', 'slack']
    assert 'SEALED_PROCESS_RESULT' not in second['row']['thread_receipts_json']
    assert second['row']['job_json'] == '{}' and second['row']['content'] == ''
