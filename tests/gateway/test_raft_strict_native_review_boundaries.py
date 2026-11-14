"""Independent review probes. Expected assertions remain red on defects."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
from aiohttp import web

from agent import secret_scope
from gateway import pending_final as pf
from gateway.config import Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from gateway.session_results import finish_result
from gateway.strict_transport import prepare_strict_transport, strict_call
from hermes_state import SessionDB
from hermes_state_runtime import admit_session_input, begin_runtime_epoch, claim_session_input
from plugins.platforms.raft.adapter import RaftAdapter
from plugins.platforms.raft import strict_delivery as sd
from tests.gateway import test_raft_strict_native_transport as transport


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['home', 'adapter_profile', 'secret_scope'])
async def test_used_plan_refuses_owner_drift(fault, tmp_path, monkeypatch):
    async with transport.peer() as wire:
        transport.credential(tmp_path, monkeypatch, wire.url)
        monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'owner-A'))
        adapter = RaftAdapter(PlatformConfig(enabled=True))
        adapter._owner_profile = 'owner-A'
        plan = await transport.prepare(adapter)
        multiplex = scope = None
        if fault == 'home':
            monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'owner-B'))
        elif fault == 'adapter_profile':
            adapter._owner_profile = 'owner-B'
        else:
            multiplex = secret_scope.set_multiplex_context(True)
            scope = secret_scope.set_secret_scope({}, profile_home=str(tmp_path / 'owner-B'))
        try:
            try:
                result = await transport.consume(adapter, plan)
            except (RuntimeError, ValueError):
                result = None
            print(json.dumps({'fault': fault, 'sent': len(wire.events), 'success': getattr(result, 'success', None)}))
            assert wire.events == [], 'owner/profile drift must be rejected before any POST'
        finally:
            if scope is not None:
                secret_scope.reset_secret_scope(scope)
                secret_scope.reset_multiplex_context(multiplex)


async def settled(tmp_path, monkeypatch, endpoint):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    transport.credential(tmp_path, monkeypatch, endpoint)
    adapter = RaftAdapter(PlatformConfig(enabled=True))
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('sid', source='raft')
    epoch = begin_runtime_epoch(db, instance_id='review-producer')
    source = SessionSource(platform=Platform('raft'), chat_id='#loopback', chat_type='group')
    route = 'agent:main:raft:group:#loopback'
    payload = {'text': 'produce once', 'native_text_v1': {'route': route, 'source': source.to_dict(),
               'automation': {'owner': 'sid', 'identity': 'review-result', 'result_recovery_v1': 'consume_only'}}}
    admit_session_input(db, epoch=epoch, principal_id='automation:' + route, session_id='sid',
                        request_id='review-result', payload=payload)
    row = claim_session_input(db, epoch=epoch, session_id='sid')
    event = MessageEvent(text='produce once', source=source, internal=True, message_id='review-result')
    authority = SimpleNamespace(db=db, runner=SimpleNamespace(_adapter_for_source=lambda source: adapter))
    descriptor = await pf.prepare(authority, row, event, route, 'sealed-raft-loopback-result')
    finish_result(db, epoch=epoch, row=row, response='sealed-raft-loopback-result', outcome='completed',
                  result={'result': {'final_response': 'sealed-raft-loopback-result', 'messages': [],
                                    '_pending_final_v1': descriptor}, 'usage': {}})
    admission = row['admission_id']
    sd._pending_value(adapter, db, admission)
    return adapter, db, admission


def mutate(db, admission, fault):
    def write(conn):
        value = pf._read(conn, admission)
        fields = {'content': ('content', 'damaged after claim'),
                  'metadata': ('metadata', {'thread_id': 'new-thread'}),
                  'adapter_profile': ('adapter_profile', 'foreign-owner')}
        if fault in fields:
            key, replacement = fields[fault]
            if key == 'content':
                value['parts']['text'][key] = replacement
            else:
                value[key] = replacement
        if fault == 'admission':
            conn.execute('UPDATE session_admissions SET outcome=? WHERE admission_id=?', ('failed', admission))
        if fault == 'generation':
            conn.execute('UPDATE sessions SET runtime_generation=runtime_generation+1 WHERE id=?', ('sid',))
        pf._put(conn, admission, value)
    db._execute_write(write)


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['metadata', 'adapter_profile', 'content', 'admission', 'generation'])
async def test_pending_rechecks_preflight_snapshot_at_claim(fault, tmp_path, monkeypatch):
    async with transport.peer() as wire:
        adapter, db, admission = await settled(tmp_path, monkeypatch, wire.url)
        original = pf.claim
        def claim(*args, **kwargs):
            mutate(db, admission, fault)
            return original(*args, **kwargs)
        monkeypatch.setattr(pf, 'claim', claim)
        try:
            result = await sd.publish_pending(adapter, db, admission)
            print(json.dumps({'phase': 'before_claim', 'fault': fault, 'sent': len(wire.events), 'success': result.success}))
            assert wire.events == [], 'changed descriptor/context must refuse before POST'
        finally:
            db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['content', 'admission', 'generation'])
async def test_pending_rechecks_sealed_payload_after_claim(fault, tmp_path, monkeypatch):
    async with transport.peer() as wire:
        adapter, db, admission = await settled(tmp_path, monkeypatch, wire.url)
        original = pf.claim
        def claim(*args, **kwargs):
            result = original(*args, **kwargs)
            assert result[1] is True, 'valid settled fixture must win real SQLite CAS'
            mutate(db, admission, fault)
            return result
        monkeypatch.setattr(pf, 'claim', claim)
        try:
            result = await sd.publish_pending(adapter, db, admission)
            with db._read_ctx() as conn:
                value = pf._read(conn, admission)
            print(json.dumps({'phase': 'after_claim', 'fault': fault, 'sent': len(wire.events),
                              'success': result.success, 'state': value['parts']['text']['state']}))
            assert wire.events == [], 'invalidated sealed result after claim must not reach transport'
            assert not result.success
        finally:
            db.close()


@pytest.mark.asyncio
async def test_pending_real_concurrent_cas_allows_one_post(tmp_path, monkeypatch):
    async with transport.peer() as wire:
        adapter, db, admission = await settled(tmp_path, monkeypatch, wire.url)
        try:
            results = await asyncio.gather(sd.publish_pending(adapter, db, admission), sd.publish_pending(adapter, db, admission))
            with db._read_ctx() as conn:
                value = pf._read(conn, admission)
            assert len(wire.events) == 1
            assert value['parts']['text']['state'] == 'delivered'
            assert value['parts']['text']['attempts'] == 1
            assert any(r.success for r in results)
        finally:
            db.close()


@pytest.mark.asyncio
async def test_claim_bound_prepare_call_requires_identity(tmp_path, monkeypatch):
    async with transport.peer() as wire:
        adapter, _db, _admission = await settled(tmp_path, monkeypatch, wire.url)
        with pytest.raises(ValueError, match='claim_identity_required'):
            from gateway.strict_transport import prepare_call
            await prepare_call(adapter, 'send', chat_id='#loopback', content='unbound')
        assert wire.events == []


@pytest.mark.asyncio
async def test_generic_probe_stays_planless_but_recovery_mints_claim_bound_plan(tmp_path, monkeypatch):
    async with transport.peer() as wire:
        adapter, db, admission = await settled(tmp_path, monkeypatch, wire.url)
        try:
            result = await prepare_strict_transport(adapter, 'send', chat_id='#loopback',
                content='sealed-raft-loopback-result', execution_id=admission, claim_id=admission + ':text')
            assert result is None
            with db._read_ctx() as conn:
                value = pf._read(conn, admission)
            plans = await pf.prepare_delivery(adapter, value, SessionSource.from_dict(value['binding']['source']))
            assert len(plans) == 1
            kind, _part, calls = plans[0]
            assert kind == 'text' and len(calls) == 1
            method, kwargs = calls[0]
            assert method == 'send'
            assert kwargs['execution_id'] and kwargs['claim_id']
            assert kwargs['prepared_plan'] is not None
            assert wire.events == []
        finally:
            db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('raw', [b'{', b'[]', b'{"ok":true,"state":"sent","messageId":"m","messageSeq":NaN}',
    b'{"ok":true,"state":"sent","messageId":"m","messageSeq":Infinity}',
    ('{"ok":true,"state":"sent","messageId":"m","messageSeq":' + '9' * 400 + '}').encode(),
    b'{"ok":true,"state":"sent","messageId":"m","messageSeq":true}',
    b'{"ok":true,"state":"sent","messageId":"m","idempotencyKey":"contradiction"}'])
async def test_ack_numeric_and_shape_refusal_has_one_post(raw, tmp_path, monkeypatch):
    events = []
    async def handle(request):
        events.append(await request.read())
        return web.Response(body=raw, content_type='application/json')
    app = web.Application()
    app.router.add_post('/internal/agent-api/v2/send', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    url = 'http://127.0.0.1:' + str(site._server.sockets[0].getsockname()[1])
    try:
        transport.credential(tmp_path, monkeypatch, url)
        adapter = RaftAdapter(PlatformConfig(enabled=True))
        plan = await transport.prepare(adapter)
        result = await transport.consume(adapter, plan)
        assert not result.success and result.retryable is False
        assert len(events) == 1
        assert 'sk_agent_' not in (result.error or '')
    finally:
        await runner.cleanup()
