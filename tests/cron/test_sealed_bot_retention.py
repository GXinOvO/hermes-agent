"""History eviction must preserve sealed Cron result consumption and provenance."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from unittest.mock import MagicMock

import pytest

from tests.cron import test_sealed_bot_consumer as bot_cases
from tests.gateway.test_session_bot_retry import _admissions, _settle_quietly

pytest_plugins = ['tests.cron.test_sealed_bot_consumer']


def _produce(bot, monkeypatch, engine):
    if engine == 'script':
        return bot_cases._produce(bot), None
    from cron import jobs, scheduler
    from tests.cron.conftest import owner_execution_runner
    agent = MagicMock()
    def respond(*args, **kwargs):
        from gateway.session_cron import current_execution
        authority, session_id, _, _ = current_execution()
        authority.db.append_message(session_id, 'assistant', 'SEALED_AGENT_REPORT')
        return {'final_response': 'SEALED_AGENT_REPORT', 'messages': []}
    agent.run_conversation.side_effect = respond
    monkeypatch.setattr('run_agent.AIAgent', lambda **kw: agent)
    monkeypatch.setattr('hermes_cli.runtime_provider.resolve_runtime_provider', lambda **kw: {
        'provider': 'openrouter', 'api_key': 'inert-fixture',
        'base_url': 'https://example.invalid/v1', 'api_mode': 'chat_completions'})
    monkeypatch.setattr('tools.mcp_tool_discovery.discover_mcp_tools', list)
    monkeypatch.setattr('cron.scheduler_authority.run_canonical_job', owner_execution_runner())
    job = jobs.create_job(prompt='inert fixture result', schedule='every 1h', deliver='bot-chat')
    assert scheduler._run_one_job_body(job)
    assert agent.run_conversation.call_count == 1
    return job, agent


def _evict(home, monkeypatch):
    from cron import executions
    monkeypatch.setattr(executions, 'MAX_TERMINAL_EXECUTIONS', 1)
    for index in range(3):
        row = executions.create_execution('unrelated-job', source='retention-control')
        assert executions.finish_execution(row['id'], success=True)
    with sqlite3.connect(home / 'cron' / 'executions.db') as db:
        assert db.execute('SELECT count(*) FROM executions').fetchone()[0] == 1


def _assert_no_replay(bot, agent):
    if agent is None:
        assert (bot.home / 'producer-calls').read_text().splitlines() == ['executed']
    else:
        assert agent.run_conversation.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('engine', ['script', 'agent'])
@pytest.mark.parametrize('deferred', [False, True])
async def test_native_seal_consumes_once_after_history_eviction(sealed_bot, monkeypatch, engine, deferred):
    from cron import delivery_queue as queue, executions, scheduler
    from cron.bot_chat_legacy import drain_legacy_pending
    from tools import bot_live_delivery as mailbox
    bot = sealed_bot
    payloads = bot_cases._wire(bot, monkeypatch)
    live = mailbox.find_canonical_live_owner
    job, agent = await asyncio.to_thread(_produce, bot, monkeypatch, engine)
    saved, row = bot_cases._queued_job(bot)
    frozen = deepcopy(saved['_sealed_bot_result_v1'])
    pending = None
    if deferred:
        monkeypatch.setattr(mailbox, 'find_canonical_live_owner',
                            lambda home: (_ for _ in ()).throw(ValueError('offline')))
        assert await asyncio.to_thread(scheduler.drain_delivery_queue, None, None) == 1
        (pending,) = (bot.home / 'cron' / 'bot_chat_pending').glob('*.json')
        assert json.loads(pending.read_text())['status'] == 'queued'
        monkeypatch.setattr(queue, 'MAX_TERMINAL_DELIVERIES', 0)
        queue.recover_abandoned()
        assert queue.get_status(row['execution_id'])['status'] == 'pending'
        assert 'job_json' in queue.get_status(row['execution_id'])
    await asyncio.to_thread(_evict, bot.home, monkeypatch)
    assert executions.get_execution(row['execution_id']) is None, 'ordinary history cap must still evict'
    monkeypatch.setattr(mailbox, 'find_canonical_live_owner', live)
    if deferred:
        await asyncio.to_thread(drain_legacy_pending)
        assert json.loads(pending.read_text())['status'] == 'transferred'
    else:
        assert await asyncio.to_thread(scheduler.drain_delivery_queue, None, None) == 1
    await _settle_quietly(bot)
    assert len(payloads) == len(_admissions(bot)) == 1, queue.get_status(row['execution_id'])
    admission = _admissions(bot)[0]
    descriptor = admission['payload']['local_automation_v1']
    reference = descriptor['sealed_cron_result_v1']
    assert descriptor['result_recovery_v1'] == 'consume_only'
    assert reference == payloads[0]['sealed_cron_result_v1']
    assert reference['execution_id'] == row['execution_id'] and reference['job_id'] == job['id']
    assert reference['seal'] == frozen['seal'] and reference['result_sha256'] == frozen['result_sha256']
    assert reference['target_home'] == str(bot.home) and reference['target_profile'] == 'default'
    assert reference['target_session_id'] == reference['target_live_session_id'] == bot.chat
    assert reference['owner_instance_id'] == bot.authority.instance_id
    assert payloads[0]['message'].endswith(row['content'])
    answer = await asyncio.to_thread(mailbox.authority_delivery, bot.home, payloads[0])
    assert answer['admission_id'] == admission['admission_id']
    await asyncio.to_thread(drain_legacy_pending)
    assert await asyncio.to_thread(scheduler.drain_delivery_queue, None, None) == 1
    assert queue.get_status(row['execution_id'])['status'] == 'delivered'
    assert await asyncio.to_thread(scheduler.drain_delivery_queue, None, None) == 0
    assert len(_admissions(bot)) == 1
    _assert_no_replay(bot, agent)
    print(json.dumps({'engine': engine, 'deferred': deferred, 'history_evicted': row['execution_id'],
                      'seal': frozen['seal'], 'consumed': admission['admission_id'], 'producer_calls': 1}))


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', ['seal', 'execution', 'owner', 'target', 'script', 'queue', 'missing-proof'])
async def test_evicted_execution_does_not_waive_provenance(sealed_bot, monkeypatch, damage):
    from cron import executions
    from cron.sealed_bot_result import _hash
    from tools import bot_live_delivery as mailbox
    bot = sealed_bot
    bot_cases._wire(bot, monkeypatch)
    await asyncio.to_thread(bot_cases._produce, bot)
    params = bot_cases._params(bot)
    job, row = bot_cases._queued_job(bot)
    await asyncio.to_thread(_evict, bot.home, monkeypatch)
    assert executions.get_execution(row['execution_id']) is None
    if damage in {'execution', 'missing-proof'}:
        proof = job['_sealed_bot_result_v1'].get('execution')
        if damage == 'execution' and proof is not None:
            proof['job_id'] = 'foreign'
        else:
            job['_sealed_bot_result_v1'].pop('execution', None)
        value = job['_sealed_bot_result_v1']
        value['seal'] = _hash({k: v for k, v in value.items() if k != 'seal'})
        params['sealed_cron_result_v1']['seal'] = value['seal']
        with sqlite3.connect(bot.home / 'cron' / 'deliveries.db') as db:
            db.execute('UPDATE deliveries SET job_json=? WHERE execution_id=?',
                       (json.dumps(job), row['execution_id']))
    elif damage == 'queue':
        with sqlite3.connect(bot.home / 'cron' / 'deliveries.db') as db:
            db.execute('UPDATE deliveries SET content=?', ('foreign',))
    elif damage == 'script':
        (bot.home / 'scripts' / 'result.py').write_text('print("changed input")')
    else:
        key = {'seal': 'seal', 'owner': 'owner_instance_id', 'target': 'target_home'}[damage]
        params['sealed_cron_result_v1'][key] = 'foreign'
    with pytest.raises(ValueError, match='admission_conflict'):
        await asyncio.to_thread(mailbox.authority_delivery, bot.home, params)
    assert not _admissions(bot)
    assert (bot.home / 'producer-calls').read_text().splitlines() == ['executed']


@pytest.mark.asyncio
@pytest.mark.parametrize('deferred', [False, True])
async def test_legacy_pending_seal_retains_only_its_required_native_row(sealed_bot, monkeypatch, deferred):
    from cron import delivery_queue as queue, executions, scheduler
    from cron.bot_chat_legacy import drain_legacy_pending
    from cron.sealed_bot_result import _hash
    from tools import bot_live_delivery as mailbox
    bot = sealed_bot
    payloads = bot_cases._wire(bot, monkeypatch)
    live = mailbox.find_canonical_live_owner
    await asyncio.to_thread(bot_cases._produce, bot)
    job, row = bot_cases._queued_job(bot)
    value = job['_sealed_bot_result_v1']
    value.pop('execution', None)
    value['seal'] = _hash({k: v for k, v in value.items() if k != 'seal'})
    with sqlite3.connect(bot.home / 'cron' / 'deliveries.db') as db:
        db.execute('UPDATE deliveries SET job_json=?', (json.dumps(job),))
    if deferred:
        monkeypatch.setattr(mailbox, 'find_canonical_live_owner',
                            lambda home: (_ for _ in ()).throw(ValueError('offline')))
        assert await asyncio.to_thread(scheduler.drain_delivery_queue, None, None) == 1
        monkeypatch.setattr(queue, 'MAX_TERMINAL_DELIVERIES', 0)
        queue.recover_abandoned()
    monkeypatch.setattr(executions, 'MAX_TERMINAL_EXECUTIONS', 1)
    for _ in range(3):
        other = executions.create_execution('ordinary-history', source='control')
        executions.finish_execution(other['id'], success=True)
    assert executions.get_execution(row['execution_id']) is not None
    with sqlite3.connect(bot.home / 'cron' / 'executions.db') as db:
        assert db.execute('SELECT count(*) FROM executions').fetchone()[0] == 2
    monkeypatch.setattr(mailbox, 'find_canonical_live_owner', live)
    if deferred:
        await asyncio.to_thread(drain_legacy_pending)
    else:
        assert await asyncio.to_thread(scheduler.drain_delivery_queue, None, None) == 1
    await _settle_quietly(bot)
    assert await asyncio.to_thread(scheduler.drain_delivery_queue, None, None) == 1
    assert len(payloads) == len(_admissions(bot)) == 1
    assert queue.get_status(row['execution_id'])['status'] == 'delivered'
    other = executions.create_execution('ordinary-history', source='control')
    executions.finish_execution(other['id'], success=True)
    assert executions.get_execution(row['execution_id']) is None


def _daemon_rows(home):
    with sqlite3.connect(home / 'state.db') as db:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute("SELECT * FROM session_admissions WHERE request_id LIKE 'bot:%'")]


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('engine', ['script', 'agent'])
def test_real_gateway_boot_consumes_evicted_result_and_reopens_receipt(tmp_path, monkeypatch, engine, record_property):
    import threading
    from http.server import ThreadingHTTPServer
    from tests.gateway.fixtures.local_recovery_probe import Model, child_env, daemon, rpc, websocket
    from tests.waiting import async_wait_until, wait_until
    from cron import delivery_queue, executions, jobs, scheduler
    from cron.scheduler_provider import _profile_cron_scope
    from tools.bot_live_delivery import authority_delivery
    from types import SimpleNamespace
    home, user = tmp_path / 'state', tmp_path / 'user'
    home.mkdir()
    user.mkdir()
    peer = ThreadingHTTPServer(('127.0.0.1', 0), Model)
    peer.requests = []
    peer.blocked, peer.release = threading.Event(), threading.Event()
    thread = threading.Thread(target=peer.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{peer.server_port}/v1'
    config = {'gateway': {'multiplex_profiles': False},
              'model': {'provider': 'custom', 'default': 'retention-fixture', 'base_url': url},
              'auxiliary': {'title_generation': {'enabled': False}}, 'model_catalog': {'enabled': False},
              'platform_toolsets': {'cli': []}, 'terminal': {'cwd': str(home)}}
    (home / 'config.yaml').write_text(json.dumps(config))
    root = Path(__file__).resolve().parents[2]
    env = child_env()
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home), PYTHONPATH=str(root),
               OPENAI_API_KEY='loopback-only', OPENAI_BASE_URL=url, PYTHONUNBUFFERED='1')
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(Path, 'home', lambda: user)
    pids = []
    try:
        with _profile_cron_scope(home):
            job, agent = _produce(SimpleNamespace(home=home), monkeypatch, engine)
            queued = bot_cases._queued_job(SimpleNamespace(home=home))[1]
            frozen = json.loads(queued['job_json'])['_sealed_bot_result_v1']
            assert jobs.get_job(job['id'])['repeat']['completed'] == 1
            _evict(home, monkeypatch)
            assert executions.get_execution(queued['execution_id']) is None
        async def observe(desc):
            await async_wait_until(lambda: len(_daemon_rows(home)) == 1 and _daemon_rows(home)[0]['status'] == 'terminal',
                                   timeout=60, what='evicted sealed Bot result consumed')
            row = _daemon_rows(home)[0]
            descriptor = json.loads(row['payload_json'])['local_automation_v1']
            reference = descriptor['sealed_cron_result_v1']
            assert descriptor['result_recovery_v1'] == 'consume_only'
            assert reference['execution_id'] == queued['execution_id'] and reference['seal'] == frozen['seal']
            assert reference['job_id'] == job['id'] and reference['target_home'] == str(home)
            assert reference['owner_instance_id'] == desc['instance_id']
            async with websocket(home, desc) as ws:
                history = await rpc(ws, 'session.resume', session_id=row['target_session_id'])
                assert 'RECOVERY_ACK_' in json.dumps(history) and queued['content'] in json.dumps(history)
            params = {'id': row['request_id'][4:], 'profile': 'default',
                      'message': json.loads(row['payload_json'])['text'], 'sealed_cron_result_v1': reference}
            if reference['target_session_id']:
                params['session_id'] = reference['target_session_id']
            return row, params
        with daemon(root, home, env, barrier=False) as (first, desc):
            pids.append(first.pid)
            with _profile_cron_scope(home):
                scheduler.drain_delivery_queue(None, None)
            admission, params = asyncio.run(observe(desc))
            first_receipt = authority_delivery(home, params)
            assert first_receipt['status'] == 'settled' and first_receipt['admission_id'] == admission['admission_id']
            assert len(peer.requests) == 1
        with daemon(root, home, env, barrier=False) as (second, _desc):
            pids.append(second.pid)
            receipt = authority_delivery(home, params)
            assert receipt['status'] == 'settled' and receipt['admission_id'] == admission['admission_id']
            assert receipt['reply'] == first_receipt['reply']
            with _profile_cron_scope(home):
                scheduler.drain_delivery_queue(None, None)
                wait_until(lambda: (delivery_queue.get_status(queued['execution_id']) or {}).get('status') == 'delivered',
                           timeout=30, what='settled Bot receipt to reach the delivery ledger')
                assert scheduler.drain_delivery_queue(None, None) == 0
            assert len(_daemon_rows(home)) == len(peer.requests) == 1
        assert pids[0] != pids[1]
        _assert_no_replay(SimpleNamespace(home=home), agent)
        record_property('semantic_evidence', json.dumps({'engine': engine, 'pids': pids,
            'history_evicted': queued['execution_id'], 'seal': frozen['seal'],
            'admission_id': admission['admission_id'], 'consumer_model_calls': len(peer.requests), 'producer_calls': 1}))
    finally:
        peer.release.set()
        peer.shutdown()
        peer.server_close()
        thread.join(timeout=5)


@pytest.mark.asyncio
async def test_cross_profile_result_keeps_identity_after_source_history_eviction(sealed_bot, monkeypatch):
    from cron import executions
    produce = bot_cases._produce
    sources = []
    def produce_then_evict(bot):
        job = produce(bot)
        _job, row = bot_cases._queued_job(bot)
        _evict(bot.home, monkeypatch)
        assert executions.get_execution(row['execution_id']) is None
        sources.append(bot.home)
        return job
    monkeypatch.setattr(bot_cases, '_produce', produce_then_evict)
    await bot_cases.test_genuine_cross_profile_producer_uses_its_native_store(sealed_bot, monkeypatch)
    assert len(sources) == 1
    assert (sources[0] / 'producer-calls').read_text().splitlines() == ['executed']


@pytest.mark.asyncio
async def test_canonical_row_missing_recovery_keeps_the_completed_bot_result(sealed_bot, monkeypatch):
    from cron import delivery_queue as queue, executions, jobs, scheduler
    from tests.cron.test_completed_result_recovery import _crash_after_result, _recover_in_fresh_reader
    bot = sealed_bot
    payloads = bot_cases._wire(bot, monkeypatch)
    script = bot.home / 'scripts' / 'report.py'
    script.parent.mkdir()
    script.write_text('from pathlib import Path\np=Path(__file__).parents[1]/"producer-calls"\n'
                      'with p.open("a") as f: f.write("executed\\n")\nprint("RECOVERED_REPORT")\n')
    job = jobs.create_job(prompt=None, script=script.name, no_agent=True, schedule='every 1h', deliver='bot-chat')
    execution_id = job['execution_id'] = executions.create_execution(job['id'], source='test')['id']
    assert executions.mark_execution_handoff_pending(execution_id)
    await asyncio.to_thread(_crash_after_result, bot.home, job, 'save')
    with sqlite3.connect(bot.home / 'cron' / 'executions.db') as db:
        db.execute('DELETE FROM executions WHERE id=?', (execution_id,))
    await asyncio.to_thread(_recover_in_fresh_reader, bot.home)
    assert queue.get_status(execution_id)['status'] == 'pending'
    assert jobs.get_job(job['id'])['repeat']['completed'] == 1
    assert await asyncio.to_thread(scheduler.drain_delivery_queue, None, None) == 1
    await _settle_quietly(bot)
    await asyncio.to_thread(_recover_in_fresh_reader, bot.home)
    assert await asyncio.to_thread(scheduler.drain_delivery_queue, None, None) == 1
    assert await asyncio.to_thread(scheduler.drain_delivery_queue, None, None) == 0
    assert len(payloads) == len(_admissions(bot)) == 1
    assert (bot.home / 'producer-calls').read_text().splitlines() == ['executed']


@pytest.mark.asyncio
async def test_old_seal_still_requires_native_execution(sealed_bot, monkeypatch):
    from cron.sealed_bot_result import _hash
    bot = sealed_bot
    payloads = bot_cases._wire(bot, monkeypatch)
    await asyncio.to_thread(bot_cases._produce, bot)
    job, _row = bot_cases._queued_job(bot)
    value = job['_sealed_bot_result_v1']
    value.pop('execution', None)
    value['seal'] = _hash({k: v for k, v in value.items() if k != 'seal'})
    with sqlite3.connect(bot.home / 'cron' / 'deliveries.db') as db:
        db.execute('UPDATE deliveries SET job_json=?', (json.dumps(job),))
    from cron import scheduler
    assert await asyncio.to_thread(scheduler.drain_delivery_queue, None, None) == 1
    await _settle_quietly(bot)
    assert len(payloads) == len(_admissions(bot)) == 1
