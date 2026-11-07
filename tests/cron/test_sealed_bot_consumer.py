"""Cron's native completed result reaches Bot admission without becoming retryable work."""
import asyncio
import json
import os
from pathlib import Path

import pytest
import pytest_asyncio

from tests.gateway.test_session_bot_retry import _admissions, _settle_quietly

pytest_plugins = ['tests.gateway.test_session_bot_retry']


@pytest_asyncio.fixture
async def sealed_bot(tmp_path, monkeypatch):
    from gateway import run
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway.session_authority import initialize_session_authority
    from gateway.session_contract import SessionActor
    from gateway.session_local import create_local_session
    from gateway.session_local_title import title_new_session
    from hermes_state import SessionDB
    from types import SimpleNamespace
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(run, '_load_gateway_config', lambda: {'platform_toolsets': {'cli': []}})
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    store._db = SessionDB(db_path=tmp_path / 'state.db')
    runner = SimpleNamespace(session_store=store, _session_db=store._db, adapters={}, _draining=False,
                             _evict_cached_agent=lambda route: None)
    runner._adapter_for_source = lambda source: runner.adapters.get(source.platform)
    authority = await initialize_session_authority(runner, profile_id=str(tmp_path), instance_id='fixture')
    owner = SessionActor(f'uid:{os.getuid()}', str(tmp_path),
                         frozenset({'session:create', 'session:read', 'session:submit'}), 'native')
    chat = create_local_session(authority, owner, {'request_id':'bot', 'source':'gui', 'cwd':str(tmp_path),
                                                 'model':'fixture', 'toolsets':[]})
    title_new_session(authority, chat, 'Bot Chat')
    errors = []

    async def execute(authority, ref, row):
        from gateway.platforms.event import MessageEvent
        from gateway.session_delivery_policy import automation_delivery_scope, arm_tool_witness
        event = MessageEvent(text='provider fixture', source=authority.sessions[ref.session_id].source)
        async with automation_delivery_scope(authority, row, event):
            arm_tool_witness(SimpleNamespace(tool_start_callback=None))
            error = errors.pop(0) if errors else None
            if error is None:
                return 'pong'
            authority.pending_results[row['admission_id']] = {
                'result':{'final_response':'', 'messages':[], 'failed':True, 'error':error}, 'usage':{}}
            return ''
    monkeypatch.setattr('gateway.session_finite.execute_finite_admission', execute)
    try:
        yield SimpleNamespace(authority=authority, chat=chat.session_id, errors=errors, home=tmp_path,
                              connection=SimpleNamespace(authority=authority, actor=owner))
    finally:
        await _settle_quietly(SimpleNamespace(authority=authority))
        store._db.close()


def _produce(bot):
    from cron import jobs, scheduler
    script = bot.home / 'scripts' / 'result.py'
    script.parent.mkdir(exist_ok=True)
    script.write_text('from pathlib import Path\np=Path(__file__).parents[1]/"producer-calls"\n'
                      'with p.open("a") as f: f.write("executed\\n")\nprint("SEALED_REPORT")\n')
    job = jobs.create_job(prompt=None, schedule='every 1h', script=script.name,
                          no_agent=True, deliver='bot-chat')
    assert scheduler._run_one_job_body(job)
    return job


def _wire(bot, monkeypatch):
    from gateway.session_controls import AuthorityConnection
    from tools import bot_live_delivery as mailbox
    loop = asyncio.get_running_loop()
    connection = AuthorityConnection(bot.authority, lambda *a: None, {
        'user_id': f'uid:{os.getuid()}', 'provider':'local', 'profile_id': bot.authority.profile_id,
        'capabilities':['session:create','session:read','session:submit'],
        'instance_id': bot.authority.instance_id, 'native_bootstrap': True}, operator=True)
    payloads = []

    def rpc(home, method, params):
        payloads.append(json.loads(json.dumps(params)))
        answer = asyncio.run_coroutine_threadsafe(connection.dispatch(
            {'id': 1, 'method': method, 'params': params}), loop).result(timeout=20)
        if 'error' in answer:
            raise ValueError(answer['error'])
        return answer['result']

    monkeypatch.setattr(mailbox, '_authority_rpc', rpc)
    monkeypatch.setattr(mailbox, 'find_canonical_live_owner', lambda home: dict(
        profile_home=str(Path(home).resolve()), session_id=bot.chat, live_session_id=bot.chat,
        lease_id=bot.authority.instance_id))
    return payloads


@pytest.mark.asyncio
async def test_scheduler_sealed_result_survives_real_door_and_provider_503_without_retry(sealed_bot, monkeypatch):
    bot = sealed_bot
    from cron import scheduler
    from gateway.session_bot import recover_bot_deliveries
    payloads = _wire(bot, monkeypatch)
    bot.errors[:] = ['HTTP 503 server error']
    await asyncio.to_thread(_produce, bot)
    assert await asyncio.to_thread(scheduler.drain_delivery_queue, None, None) == 1
    await _settle_quietly(bot)
    await recover_bot_deliveries(bot.authority)
    await _settle_quietly(bot)
    rows = _admissions(bot)
    with bot.authority.db._read_ctx() as conn:
        admissions = [dict(r) for r in conn.execute('SELECT * FROM session_admissions')]
    import sqlite3
    with sqlite3.connect(bot.home / 'cron' / 'deliveries.db') as conn:
        queue = list(conn.execute('SELECT status, error FROM deliveries'))
    assert len(rows) == 1, (admissions, payloads, queue)
    descriptor = rows[0]['payload']['local_automation_v1']
    assert descriptor['result_recovery_v1'] == 'consume_only'
    assert descriptor['sealed_cron_result_v1'] == payloads[0]['sealed_cron_result_v1']
    assert descriptor['sealed_cron_result_v1']['result_sha256']
    again = await asyncio.to_thread(__import__('tools.bot_live_delivery', fromlist=['authority_delivery']).authority_delivery,
                                    bot.home, payloads[0])
    assert again['admission_id'] == rows[0]['admission_id'] and len(_admissions(bot)) == 1
    assert (bot.home / 'producer-calls').read_text().splitlines() == ['executed']


def _queued_job(bot):
    from cron.sealed_bot_result import _row
    import sqlite3
    with sqlite3.connect(bot.home / 'cron' / 'deliveries.db') as conn:
        execution_id = conn.execute('SELECT execution_id FROM deliveries').fetchone()[0]
    row = _row(bot.home, execution_id)
    return json.loads(row['job_json']), row


def _params(bot):
    from cron.sealed_bot_result import producer_reference, delivery_identity
    from cron.scheduler_delivery import bot_chat_message
    job, row = _queued_job(bot)
    owner = dict(session_id=bot.chat, live_session_id=bot.chat, lease_id=bot.authority.instance_id)
    ref = producer_reference(job, row['content'], bot.home, owner)
    return dict(id=delivery_identity(bot.home, job['id'], row['execution_id'], bot.home),
                profile='default', message=bot_chat_message(job, row['content']), session_id=bot.chat,
                sealed_cron_result_v1=ref)


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [
    ('source_profile', '../../foreign'), ('source_profile','missing'),
    ('seal', '0'*64), ('result_sha256','1'*64), ('content_sha256','2'*64),
    ('input_sha256','3'*64), ('job_id','foreign'), ('execution_id','foreign'),
    ('target_profile','foreign'), ('target_home','/untrusted'),
    ('target_session_id','foreign'), ('target_live_session_id','foreign'),
    ('owner_instance_id','old-owner'), ('strict',True), ('version',True),
])
async def test_forged_reference_rejected_before_admission(sealed_bot, monkeypatch, field, value):
    from tools import bot_live_delivery as mailbox
    bot = sealed_bot
    _wire(bot, monkeypatch)
    await asyncio.to_thread(_produce, bot)
    params = _params(bot)
    params['sealed_cron_result_v1'][field] = value
    with pytest.raises(ValueError, match='admission_conflict'):
        await asyncio.to_thread(mailbox.authority_delivery, bot.home, params)
    assert not _admissions(bot)


@pytest.mark.asyncio
@pytest.mark.parametrize('drift', ['message','session','script','job','target','native-result','queue-content'])
async def test_native_input_result_and_target_drift_refuse_consumption(sealed_bot, monkeypatch, drift):
    from cron import jobs
    from tools import bot_live_delivery as mailbox
    bot = sealed_bot
    _wire(bot, monkeypatch)
    await asyncio.to_thread(_produce, bot)
    params = _params(bot)
    job, row = _queued_job(bot)
    def change_queue(column, value):
        import sqlite3
        with sqlite3.connect(bot.home / 'cron' / 'deliveries.db') as conn:
            conn.execute(f'UPDATE deliveries SET {column}=? WHERE execution_id=?', (value, row['execution_id']))

    def replace_result():
        job['_sealed_bot_result_v1']['result'][2] = 'replaced'
        change_queue('job_json', json.dumps(job))

    mutations = {
        'message':lambda:params.update(message=params['message'] + 'changed'),
        'session':lambda:params.update(session_id='foreign'),
        'script':lambda:(bot.home / 'scripts' / 'result.py').write_text('print("changed")'),
        'job':lambda:jobs.update_job(job['id'], {'prompt':'changed'}),
        'target':lambda:jobs.update_job(job['id'], {'deliver':'local'}),
        'native-result':replace_result,
        'queue-content':lambda:change_queue('content','replaced'),
    }
    mutations[drift]()
    with pytest.raises(ValueError, match='admission_conflict'):
        await asyncio.to_thread(mailbox.authority_delivery, bot.home, params)
    assert not _admissions(bot)
    assert (bot.home / 'producer-calls').read_text().splitlines() == ['executed']


@pytest.mark.asyncio
@pytest.mark.parametrize('native', [False, True])
async def test_non_native_or_unprivileged_rpc_cannot_self_select_strict(sealed_bot, native):
    from gateway.session_controls import AuthorityConnection
    bot = sealed_bot
    await asyncio.to_thread(_produce, bot)
    connection = AuthorityConnection(bot.authority, lambda *a: None, {
        'user_id': f'uid:{os.getuid()}', 'provider':'local', 'profile_id':bot.authority.profile_id,
        'instance_id':bot.authority.instance_id, 'capabilities':['session:read','session:submit'],
        'native_bootstrap':native}, operator=False)
    response = await connection.dispatch({'id':1, 'method':'bot_relay.deliver', 'params':_params(bot)})
    assert 'error' in response, response
    assert not _admissions(bot)


@pytest.mark.asyncio
@pytest.mark.parametrize('lost_reply', [False, True])
async def test_down_target_keeps_same_seal_and_never_reconsumes_unknown(sealed_bot, monkeypatch, lost_reply):
    from cron import scheduler
    from cron.bot_chat_legacy import drain_legacy_pending
    from tools import bot_live_delivery as mailbox
    bot = sealed_bot
    payloads = _wire(bot, monkeypatch)
    live = mailbox.find_canonical_live_owner
    monkeypatch.setattr(mailbox, 'find_canonical_live_owner', lambda home: (_ for _ in ()).throw(ValueError('offline')))
    await asyncio.to_thread(_produce, bot)
    assert await asyncio.to_thread(scheduler.drain_delivery_queue, None, None) == 1
    (pending,) = (bot.home / 'cron' / 'bot_chat_pending').glob('*.json')
    original = json.loads(pending.read_text())
    await asyncio.to_thread(drain_legacy_pending)
    assert not payloads and json.loads(pending.read_text())['status'] == 'queued'
    monkeypatch.setattr(mailbox, 'find_canonical_live_owner', live)
    if lost_reply:
        deliver = mailbox.authority_delivery
        def lose_after_accept(home, params):
            deliver(home, params)
            raise ValueError('lost acknowledgement')
        monkeypatch.setattr(mailbox, 'authority_delivery', lose_after_accept)
    await asyncio.to_thread(drain_legacy_pending)
    await _settle_quietly(bot)
    await asyncio.to_thread(drain_legacy_pending)
    assert len(payloads) == len(_admissions(bot)) == 1
    saved = json.loads(pending.read_text())
    assert saved['status'] == ('ambiguous' if lost_reply else 'transferred')
    assert payloads[0]['sealed_cron_result_v1']['seal'] == original['job']['_sealed_bot_result_v1']['seal']
    assert _admissions(bot)[0]['payload']['local_automation_v1']['result_recovery_v1'] == 'consume_only'
    assert (bot.home / 'producer-calls').read_text().splitlines() == ['executed']


async def _cross_profile_case(bot, monkeypatch, source_home):
    from cron import jobs, scheduler
    from cron.scheduler_provider import _profile_cron_scope
    from types import SimpleNamespace
    from tools import bot_live_delivery as mailbox
    loop = asyncio.get_running_loop()
    from gateway.session_controls import AuthorityConnection
    connection = AuthorityConnection(bot.authority, lambda *a: None, {
        'user_id': f'uid:{os.getuid()}', 'provider':'local', 'profile_id':bot.authority.profile_id,
        'instance_id':bot.authority.instance_id, 'capabilities':['session:create','session:read','session:submit'],
        'native_bootstrap':True}, operator=True)
    def rpc(home, method, params):
        response = asyncio.run_coroutine_threadsafe(connection.dispatch({'id':1,'method':method,'params':params}), loop).result(20)
        assert 'result' in response, response
        return response['result']
    monkeypatch.setattr(mailbox, '_authority_rpc', rpc)
    monkeypatch.setattr(mailbox, 'find_canonical_live_owner', lambda home: dict(
        session_id=bot.chat, live_session_id=bot.chat, profile_home=str(home), lease_id=bot.authority.instance_id))
    def produce():
        with _profile_cron_scope(source_home):
            return _produce(SimpleNamespace(home=source_home))
    await asyncio.to_thread(produce)
    def drain():
        with _profile_cron_scope(source_home):
            job = jobs.load_jobs()[0]
            # Target was frozen by the real producer, before the canonical enqueue.
            assert job['deliver'] == 'bot-chat:target'
            assert scheduler.drain_delivery_queue(None, None) == 1
        import sqlite3
        with sqlite3.connect(source_home / 'cron' / 'deliveries.db') as conn:
            return list(conn.execute('SELECT status,error FROM deliveries'))
    outcomes = await asyncio.to_thread(drain)
    await _settle_quietly(bot)
    assert len(_admissions(bot)) == 1, outcomes
    descriptor = _admissions(bot)[0]['payload']['local_automation_v1']
    assert descriptor['result_recovery_v1'] == 'consume_only'
    assert descriptor['sealed_cron_result_v1']['source_profile'] == 'producer'
    assert descriptor['sealed_cron_result_v1']['target_profile'] == 'target'


@pytest.mark.asyncio
async def test_genuine_cross_profile_producer_uses_its_native_store(sealed_bot, monkeypatch):
    from cron import jobs
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway.session_authority import initialize_session_authority
    from gateway.session_contract import SessionActor
    from gateway.session_local import create_local_session
    from gateway.session_local_title import title_new_session
    from hermes_state import SessionDB
    from types import SimpleNamespace
    root = sealed_bot.home
    source, target = root / 'profiles' / 'producer', root / 'profiles' / 'target'
    source.mkdir(parents=True)
    target.mkdir()
    for home in (source,target):
        (home / 'config.yaml').write_text('platform_toolsets:\n  cli: []\n')
    store = SessionStore(target / 'sessions', GatewayConfig())
    store._db = SessionDB(db_path=target / 'state.db')
    runner = SimpleNamespace(session_store=store, _session_db=store._db, adapters={}, _draining=False,
                             _evict_cached_agent=lambda route: None)
    runner._adapter_for_source = lambda source: runner.adapters.get(source.platform)
    authority = await initialize_session_authority(runner, profile_id=str(target), instance_id='target-owner')
    owner = SessionActor(f'uid:{os.getuid()}', str(target), frozenset({'session:create','session:read','session:submit'}),'native')
    from gateway.session_authorities import owner_scope
    with owner_scope(authority):
        chat = create_local_session(authority, owner, {'request_id':'cross-bot','source':'gui','cwd':str(target),
                                                     'model':'fixture','toolsets':[]})
        title_new_session(authority, chat, 'Bot Chat')
    create = jobs.create_job
    monkeypatch.setattr(jobs, 'create_job', lambda **kw:create(**{**kw, 'deliver':'bot-chat:target'}))
    try:
        await _cross_profile_case(SimpleNamespace(authority=authority, home=target, chat=chat.session_id), monkeypatch, source)
    finally:
        await _settle_quietly(SimpleNamespace(authority=authority))
        store._db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['stripped-provenance','producer-target-home','deferred-target-home'])
async def test_native_producer_cannot_downgrade_or_redirect_result(sealed_bot, monkeypatch, kind):
    from cron.sealed_bot_result import producer_reference, delivery_identity
    from cron.bot_chat_legacy import defer, drain_legacy_pending
    from tools import bot_live_delivery as mailbox
    bot = sealed_bot
    await asyncio.to_thread(_produce, bot)
    job, row = _queued_job(bot)
    owner = dict(session_id=bot.chat, live_session_id=bot.chat, lease_id=bot.authority.instance_id)
    def stripped():
        job.pop('_sealed_bot_result_v1')
        job.pop('_cron_result_binding')
        with pytest.raises(ValueError, match='provenance missing'):
            producer_reference(job, row['content'], bot.home, owner)

    def producer_target():
        with pytest.raises(ValueError, match='target home drift'):
            producer_reference(job, row['content'], bot.home / 'outside', owner)

    def deferred_target():
        key = delivery_identity(bot.home, job['id'], row['execution_id'], bot.home)
        defer(key, job, row['content'], bot.home, for_failure=False)
        path = bot.home / 'cron' / 'bot_chat_pending' / (key + '.json')
        record = json.loads(path.read_text())
        record['home'] = str(bot.home / 'outside')
        path.write_text(json.dumps(record))
        monkeypatch.setattr(mailbox, 'find_canonical_live_owner', lambda home: pytest.fail('target drift must not dial'))
        drain_legacy_pending()
        assert json.loads(path.read_text())['status'] == 'ambiguous'

    {'stripped-provenance':stripped, 'producer-target-home':producer_target,
     'deferred-target-home':deferred_target}[kind]()
    assert not _admissions(bot)
    assert (bot.home / 'producer-calls').read_text().splitlines() == ['executed']


def test_metadata_and_result_words_cannot_mint_a_runtime_seal():
    from cron.sealed_bot_result import bind_event, event_descriptor
    from types import SimpleNamespace
    event = SimpleNamespace(metadata={'sealed_cron_result_v1':{'seal':'pretend'}, 'consume_only':True},
                            text='sealed consume-only producer')
    assert event_descriptor(event) == {}
    with pytest.raises(ValueError, match='untrusted'):
        bind_event(event, {'reference':{'seal':'pretend'}})


def test_legacy_pending_without_seal_is_unknown_without_dial(tmp_path, monkeypatch):
    from cron.bot_chat_legacy import defer, drain_legacy_pending
    from tools import bot_live_delivery as mailbox
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    defer('e'*64, {'id':'legacy'}, 'old result', tmp_path, for_failure=False)
    monkeypatch.setattr(mailbox, 'find_canonical_live_owner', lambda home: pytest.fail('unsealed replay must not dial'))
    drain_legacy_pending()
    drain_legacy_pending()
    (path,) = (tmp_path / 'cron' / 'bot_chat_pending').glob('*.json')
    assert json.loads(path.read_text())['status'] == 'ambiguous'
