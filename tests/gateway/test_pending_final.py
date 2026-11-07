"""Regression for the settled-result / first-send crash window."""
from concurrent.futures import ThreadPoolExecutor

import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from hermes_state import SessionDB
from hermes_state_runtime import admit_session_input, begin_runtime_epoch, claim_session_input
from gateway.strict_transport import single_attempt_transport


class Adapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True), Platform.SLACK)

    async def connect(self, **kwargs):
        return True

    async def disconnect(self):
        pass

    async def get_chat_info(self, chat_id):
        return {}

    @single_attempt_transport
    async def send(self, chat_id, content, **kwargs):
        return SendResult(success=True)


async def _sealed(tmp_path, *, human=False):
    from gateway.pending_final import prepare
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('sid', source='slack')
    epoch = begin_runtime_epoch(db, instance_id='owner')
    route = 'agent:main:slack:dm:chat'
    source = SessionSource(platform=Platform.SLACK, chat_id='chat', chat_type='dm')
    payload = {'text':'saved task', 'native_text_v1':{'route':route, 'source':source.to_dict(),
        'automation':{'owner':'sid','identity':'task','result_recovery_v1':'consume_only'}}}
    if human:
        payload['native_text_v1'].pop('automation')
        payload['native_text_v1']['source']['is_bot'] = False
    admit_session_input(db, epoch=epoch, principal_id='messaging:sender' if human else 'automation:' + route,
                        session_id='sid', request_id='task', payload=payload)
    row = claim_session_input(db, epoch=epoch, session_id='sid')
    event = MessageEvent(text='saved task', source=source, internal=True, message_id='task')
    adapter = Adapter()
    authority = SimpleNamespace(db=db, runner=SimpleNamespace(_adapter_for_source=lambda source:adapter))
    prepared = await prepare(authority, row, event, route, 'sealed final')
    return db, epoch, row, event, prepared, authority


@pytest.mark.asyncio
async def test_terminal_commit_contains_exact_pending_final(tmp_path):
    from gateway.pending_final import pending
    from gateway.session_results import finish_result
    db, epoch, row, _event, prepared, _authority = await _sealed(tmp_path)
    try:
        with pytest.raises(RuntimeError, match='abort'):
            def failing(conn, raw):
                from gateway.pending_final import persist
                persist(conn, raw, prepared, db_path=db.db_path)
                raise RuntimeError('abort')
            from hermes_state_runtime import settle_session_input
            settle_session_input(db, epoch=epoch, admission_id=row['admission_id'], generation=row['generation'],
                                 outcome='completed', _terminal_write=failing)
        assert pending(db) == []
        finish_result(db, epoch=epoch, row=row, response='sealed final', outcome='completed',
                      result={'result':{'final_response':'sealed final','messages':[], '_pending_final_v1':prepared}, 'usage':{}})
        values = pending(db)
        assert len(values) == 1
        assert values[0]['binding']['admission_id'] == row['admission_id']
        assert values[0]['binding']['generation'] == row['generation']
        assert values[0]['binding']['profile_home'] == str(tmp_path.resolve())
        assert values[0]['parts']['text']['content'] == 'sealed final'
        assert values[0]['parts']['text']['state'] == 'pending'
    finally:
        db.close()


def _reader(home, mode=None):
    env = {k:v for k,v in os.environ.items() if k in {'PATH','LANG','TZ'}}
    env.update(HERMES_HOME=str(home), HOME=str(home / 'user'), PYTHONPATH=str(Path(__file__).resolve().parents[2]))
    peer = Path(__file__).parent / 'fixtures/pending_final_reader.py'
    result = subprocess.run([sys.executable, str(peer), str(home), *([mode] if mode else [])], env=env, capture_output=True,
                            text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout.splitlines()[-1])


@pytest.mark.asyncio
async def test_real_readers_refuse_live_owner_then_only_one_cas_wins(tmp_path):
    from gateway import pending_final as pf
    from gateway.session_results import finish_result
    db, epoch, row, _event, descriptor, _authority = await _sealed(tmp_path)
    try:
        finish_result(db, epoch=epoch, row=row, response='sealed final', outcome='completed',
            result={'result':{'final_response':'sealed final','messages':[], '_pending_final_v1':descriptor}, 'usage':{}})
        assert _reader(tmp_path) == {'claimed':False, 'state':'live_owner'}
        home = tmp_path / 'dead-writer'
        assert _reader(home, 'seal') == {'sealed':True}
        with ThreadPoolExecutor(max_workers=4) as pool:
            replies = list(pool.map(_reader, [home]*4))
        assert sum(reply['claimed'] for reply in replies) == 1, replies
        assert {reply['state'] for reply in replies} <= {'attempting','unknown'}
        with SessionDB(home / 'state.db') as fresh:
            value = pf.pending(fresh)[0]
            assert value['parts']['text']['attempts'] == 1
            assert pf.claim(fresh, value['binding']['admission_id'], 'text', 'sealed final', recovering=True)[1:] == (False, 'unknown')
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('drift', ['payload', 'generation', 'profile', 'target', 'unknown', 'unsupported'])
async def test_pending_final_never_sends_drift_or_uncertain_part(tmp_path, drift):
    from gateway import pending_final as pf
    from gateway.session_results import finish_result
    from gateway.session_delivery_policy import automation_delivery_scope
    from gateway.final_delivery import send_text
    db, epoch, row, event, descriptor, authority = await _sealed(tmp_path)
    try:
        async with automation_delivery_scope(authority, row, event):
            pass
        finish_result(db, epoch=epoch, row=row, response='sealed final', outcome='completed',
            result={'result':{'final_response':'sealed final','messages':[], '_pending_final_v1':descriptor}, 'usage':{}})
        if drift == 'payload':
            assert pf.claim(db, row['admission_id'], 'text', 'changed')[1:] == (False, 'payload_mismatch')
        elif drift in {'generation','profile','target'}:
            def mutate(conn):
                value = pf._read(conn, row['admission_id'])
                key = {'generation':'generation', 'profile':'profile_home', 'target':'session_id'}[drift]
                value['binding'][key] = 99 if drift=='generation' else 'foreign'
                pf._put(conn, row['admission_id'], value)
            db._execute_write(mutate)
            assert pf.claim(db, row['admission_id'], 'text', 'sealed final')[1:] == (False, 'invalidated')
        elif drift == 'unknown':
            def uncertain(conn):
                value = pf._read(conn, row['admission_id'])
                value['parts']['text']['state'] = 'unknown'
                pf._put(conn, row['admission_id'], value)
            db._execute_write(uncertain)
            assert pf.claim(db, row['admission_id'], 'text', 'sealed final')[1:] == (False, 'unknown')
        else:
            class Unsupported(Adapter):
                async def send(self, *args, **kwargs):
                    raise AssertionError('unsupported transport was invoked')
            result = await send_text(Unsupported(), event, descriptor['binding']['route'], 'sealed final', reply_to=None, metadata={})
            assert result.success is False
            assert pf.pending(db)[0]['parts']['text']['state'] == 'pending'
            assert pf.pending(db)[0]['parts']['text']['attempts'] == 0
    finally:
        db.close()


@pytest.mark.parametrize('principal,extra', [('automation:route',{}), ('messaging:sender', {'is_bot':True}),
    ('legacy',{}), ('messaging:sender',{'automation':{}})])
def test_no_legacy_or_automation_is_upgraded_to_human(principal, extra):
    from gateway.session_delivery_policy import _row_policy
    source = {'is_bot':extra.get('is_bot',False)}
    env = {'route':'route','source':source}
    if 'automation' in extra:
        env['automation'] = extra['automation']
    assert _row_policy({'principal_id':principal,'payload':{'native_text_v1':env}}) == 'legacy_unknown'


@pytest.mark.asyncio
async def test_expired_payload_is_a_permanent_tombstone_and_cannot_be_resealed(tmp_path, monkeypatch):
    from gateway import pending_final as pf
    from gateway.session_results import finish_result
    db, epoch, row, _event, descriptor, _authority = await _sealed(tmp_path)
    try:
        finish_result(db, epoch=epoch, row=row, response='sealed final', outcome='completed',
            result={'result':{'final_response':'sealed final','messages':[], '_pending_final_v1':descriptor}, 'usage':{}})
        created = pf.pending(db)[0]['created_at']
        monkeypatch.setattr(pf.time, 'time', lambda:created+pf.RETENTION_SECONDS+1)
        monkeypatch.setattr('gateway.delivery_ledger._owner_alive', lambda pid, started:False)
        pf.reconcile_orphans(db)
        value = pf.pending(db)[0]
        assert value['expired'] and 'response' not in value and 'content' not in value['parts']['text']
        assert pf.claim(db, row['admission_id'], 'text', 'sealed final')[1:] == (False,'expired')
        with pytest.raises(RuntimeError, match='already sealed'):
            db._execute_write(lambda conn:pf.persist(conn, row, descriptor, db_path=db.db_path))
    finally:
        db.close()


@pytest.mark.asyncio
async def test_result_json_cannot_invent_a_descriptor_capability(tmp_path):
    from gateway.pending_final import pending
    from gateway.session_results import finish_result
    db, epoch, row, _event, descriptor, _authority = await _sealed(tmp_path)
    try:
        untrusted = json.loads(json.dumps(descriptor))
        finish_result(db, epoch=epoch, row=row, response='sealed final', outcome='completed',
            result={'result':{'final_response':'sealed final','messages':[], '_pending_final_v1':untrusted}, 'usage':{}})
        assert pending(db) == []
    finally:
        db.close()


@pytest.mark.asyncio
async def test_mutation_boundary_invalidates_pending_even_if_generation_is_unchanged(tmp_path):
    from gateway import pending_final as pf
    from gateway.session_results import finish_result
    from hermes_state_background import invalidate_target
    db, epoch, row, _event, descriptor, _authority = await _sealed(tmp_path)
    try:
        finish_result(db, epoch=epoch, row=row, response='sealed final', outcome='completed',
            result={'result':{'final_response':'sealed final','messages':[], '_pending_final_v1':descriptor}, 'usage':{}})
        db._execute_write(lambda conn:invalidate_target(conn, 'sid'))
        assert pf.claim(db, row['admission_id'], 'text', 'sealed final')[1:] == (False,'invalidated')
    finally:
        db.close()


@pytest.mark.asyncio
async def test_stale_epoch_cannot_claim_or_finalize_a_final(tmp_path):
    from gateway import pending_final as pf
    from gateway.session_results import finish_result
    db, epoch, row, _event, descriptor, _authority = await _sealed(tmp_path)
    try:
        finish_result(db, epoch=epoch, row=row, response='sealed final', outcome='completed',
            result={'result':{'final_response':'sealed final','messages':[], '_pending_final_v1':descriptor}, 'usage':{}})
        assert pf.claim(db, row['admission_id'], 'text', 'sealed final')[1:] == (True,'attempting')
        begin_runtime_epoch(db, instance_id='successor')
        with pytest.raises(RuntimeError, match='epoch changed'):
            pf.finish(db, row['admission_id'], 'text', SendResult(success=True))
        assert pf.claim(db, row['admission_id'], 'text', 'sealed final')[1:] == (False,'stale_epoch')
    finally:
        db.close()


@pytest.mark.asyncio
async def test_explicit_human_failure_hands_back_to_existing_reconnect_retry(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from gateway import delivery_ledger as dl, pending_final as pf
    from gateway.platforms.final_reply_ledger import record, finalize
    from gateway.session_delivery_policy import automation_delivery_scope
    from gateway.session_results import finish_result
    db, epoch, row, event, descriptor, authority = await _sealed(tmp_path, human=True)
    monkeypatch.setattr(dl, '_db_path', lambda:tmp_path / 'outbound.db')
    adapter = Adapter()
    adapter.gateway_runner = SimpleNamespace(_redeliver_failed_obligations_for_platform=AsyncMock(return_value=1))
    adapter._final_delivery_adapter = lambda source:object()
    try:
        async with automation_delivery_scope(authority, row, event):
            pass
        finish_result(db, epoch=epoch, row=row, response='sealed final', outcome='completed',
            result={'result':{'final_response':'sealed final','messages':[], '_pending_final_v1':descriptor}, 'usage':{}})
        receipt = await record(adapter, event, descriptor['binding']['route'], 'sealed final', adapter, False)
        assert receipt[2] is True
        await finalize(adapter, receipt, SendResult(success=False, error='send_path_degraded'), event, adapter)
        assert pf.pending(db)[0]['parts']['text']['state'] == 'unknown'
        adapter.gateway_runner._redeliver_failed_obligations_for_platform.assert_awaited_once()
        rows = dl.sweep_failed_for_runtime('slack', profile=None)
        assert len(rows) == 1 and rows[0]['content'] == 'sealed final'
        with dl._connect() as conn:
            policy, provenance = conn.execute('SELECT recovery_policy,provenance_json FROM delivery_obligations').fetchone()
        assert policy == 'human_reply_v1' and json.loads(provenance) == descriptor['binding']
    finally:
        db.close()
