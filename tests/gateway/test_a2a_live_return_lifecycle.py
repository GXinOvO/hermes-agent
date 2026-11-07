"""Native first-response retirement, accounting and lawful-history controls."""
import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from gateway import pending_final as pf
from gateway.config import GatewayConfig, PlatformConfig
from gateway.final_delivery import send_text
from gateway.session import SessionStore
from gateway.session_authority import LiveSession, SessionAuthority
from gateway.session_controls import AuthorityConnection
from gateway.session_delivery_policy import automation_delivery_scope
from gateway.session_results import finish_result
from hermes_state import SessionDB
from hermes_state_runtime import admit_session_input, begin_runtime_epoch, claim_session_input
from plugins.platforms.a2a import durable_tasks, protocol
from plugins.platforms.a2a.adapter import A2AAdapter
from tests.gateway.a2a_durable_peer import rpc


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['SendMessage', 'message/stream'])
@pytest.mark.parametrize('policy', ['strict', 'human'])
@pytest.mark.parametrize('operation', ['unchanged', 'next-turn', 'delete', 'rewind', 'session-store-reset', 'input-required'])
async def test_first_reply_preserves_native_target_and_accounting(tmp_path, monkeypatch, method, policy, operation):
    user = tmp_path / 'user'
    user.mkdir()
    monkeypatch.setenv('HOME', str(user))
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('A2A_REPLY_TIMEOUT', '1')
    db = SessionDB(tmp_path / 'state.db')
    epoch = begin_runtime_epoch(db, instance_id='live-response-owner')
    runner = SimpleNamespace(_draining=False, _evict_cached_agent=lambda key: None)
    authority = SessionAuthority(runner, profile_id=str(tmp_path), instance_id='live-response-owner', db=db, epoch=epoch)
    adapter = A2AAdapter(PlatformConfig(enabled=True, extra={'port': 0}))
    runner._adapter_for_source = lambda source: adapter
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    store._db = db
    resolved, release, published = threading.Event(), threading.Event(), threading.Event()
    identities, executions, pushes = {}, [], []
    original_await = adapter._await_reply
    marker = 'FIRST_NATIVE_RESULT'
    content = protocol.INPUT_REQUIRED_MARKER + ' ' + marker if operation == 'input-required' else marker

    def held(pending, *args, **kwargs):
        reply = original_await(pending, *args, **kwargs)
        if pending['context_id'] == 'target-context':
            assert marker in reply[1]
            resolved.set()
            assert release.wait(20)
        return reply

    monkeypatch.setattr(adapter, '_await_reply', held)
    monkeypatch.setattr(adapter, '_send_push_notification', lambda *args, **kwargs: pushes.append(args))

    async def produce(event):
        executions.append(event.message_id)
        target = event.source.chat_id == 'target-context'
        body = content if target else 'HEALTHY_NATIVE_SIBLING'
        if target:
            entry = store.get_or_create_session(event.source)
            sid, route = entry.session_id, entry.session_key
        else:
            sid, route = 'healthy-native-sid', 'agent:main:a2a:dm:' + event.source.chat_id
            db.create_session(sid, source='a2a', session_key=route)
        db.append_message(sid, role='user', content='native mutation target')
        authority.sessions[sid] = LiveSession(None, route)
        envelope = {'route': route, 'source': {**event.source.to_dict(), 'is_bot': False},
                    'event': {}, 'timestamp': '1970-01-01T00:00:00+00:00', 'ingress_sha256': event._ingress_sha256}
        if policy == 'strict':
            envelope['automation'] = {'owner': sid, 'identity': event.message_id, 'result_recovery_v1': 'consume_only'}
        principal = ('automation:' if policy == 'strict' else 'messaging:') + route
        admit_session_input(db, epoch=epoch, principal_id=principal, session_id=sid,
                            request_id=event.message_id, payload={'text': event.text, 'native_text_v1': envelope})
        row = claim_session_input(db, epoch=epoch, session_id=sid)
        async with automation_delivery_scope(authority, row, event):
            prepared = await pf.prepare(authority, row, event, route, body)
        finish_result(db, epoch=epoch, row=row, response=body, outcome='completed',
                      result={'result': {'final_response': body, 'messages': [], '_pending_final_v1': prepared}, 'usage': {}})
        if policy == 'strict':
            result = await send_text(adapter, event, route, body, reply_to=event.message_id, metadata={'notify': True})
        else:
            result = await adapter.send(event.source.chat_id, body, reply_to=event.message_id, metadata={'notify': True})
        assert result.success, result.error
        if target:
            identities.update(task=event.message_id, session=sid, admission=row['admission_id'], route=route, event=event)
            published.set()

    adapter._message_handler = produce
    reader = owner = None
    try:
        assert await adapter.connect()
        url = f'http://127.0.0.1:{adapter._httpd.server_port}/'
        reader = asyncio.create_task(asyncio.to_thread(rpc, url, method, {'message': {
            'messageId': 'target-input', 'contextId': 'target-context', 'parts': [{'text': 'once'}]}}))
        assert await asyncio.to_thread(resolved.wait, 15)
        assert await asyncio.to_thread(published.wait, 15)
        # Finish a sibling while the original HTTP worker remains paused.
        healthy = await asyncio.to_thread(rpc, url, 'SendMessage', {'message': {
            'messageId': 'healthy-input', 'contextId': 'healthy-context', 'parts': [{'text': 'healthy'}]}})
        healthy_id = json.loads(healthy)['result']['task']['id']
        assert 'HEALTHY_NATIVE_SIBLING' in healthy
        mutation = None
        if operation in {'delete', 'rewind'}:
            sid = identities['session']
            current = db.get_session(sid)
            payload = {'target_message_id': db.get_messages(sid)[0]['id']} if operation == 'rewind' else {}
            owner = AuthorityConnection(authority, object(), {'user_id': 'human'})
            mutation = await owner.dispatch({'id': 1, 'method': 'session.mutate', 'params': {
                'session_id': sid, 'request_id': 'first-reply-' + operation,
                'expected_revision': current['runtime_revision'], 'expected_generation': current['runtime_generation'],
                'operation': operation, 'payload': payload}})
            assert 'result' in mutation, mutation
        elif operation == 'session-store-reset':
            reset = store.reset_session(identities['route'])
            assert reset.session_id != identities['session']
            assert db.get_session(identities['session'])['end_reason'] == 'session_reset'
            mutation = {'canonical_session_store_reset': reset.session_id}
        elif operation == 'next-turn':
            admit_session_input(db, epoch=epoch, principal_id='messaging:' + identities['route'],
                session_id=identities['session'], request_id='following-input', payload={'text': 'following turn',
                'native_text_v1': {'route': identities['route'], 'source': identities['event'].source.to_dict()}})
            following = claim_session_input(db, epoch=epoch, session_id=identities['session'])
            finish_result(db, epoch=epoch, row=following, response='FOLLOWING_REPLY', outcome='completed',
                          result={'result': {'final_response': 'FOLLOWING_REPLY', 'messages': []}, 'usage': {}})
        visible = operation in {'unchanged', 'next-turn', 'input-required'}
        # Snapshot after lawful mutation, before the first-response/fallback boundary.
        with db._read_ctx() as conn:
            before = conn.execute('SELECT value FROM state_meta WHERE key=?', (durable_tasks._key(identities['task']),)).fetchone()[0]
            messages_before = [tuple(row) for row in conn.execute('SELECT id,session_id,role,content FROM messages ORDER BY id')]
        metrics_before = protocol.metrics.snapshot()
        metrics_before.pop('uptime_seconds')
        pushes_before = list(pushes)
        conversation = tmp_path / 'a2a_conversations/target-context.jsonl'
        conversation_before = conversation.read_bytes()
        release.set()
        wire = await asyncio.wait_for(reader, 20)
        with db._read_ctx() as conn:
            after = conn.execute('SELECT value FROM state_meta WHERE key=?', (durable_tasks._key(identities['task']),)).fetchone()[0]
            messages_after = [tuple(row) for row in conn.execute('SELECT id,session_id,role,content FROM messages ORDER BY id')]
        state = next(v for v in pf.pending(db) if v['reply_to'] == identities['task'])['parts']['text']
        public = {name: await asyncio.to_thread(rpc, url, name, {'taskId': identities['task'], 'includeArtifacts': True})
                  for name in ['GetTask', 'SubscribeToTask']}
        healthy_get = await asyncio.to_thread(rpc, url, 'GetTask', {'taskId': healthy_id})
        facts = {'method': method, 'policy': policy, 'operation': operation, 'mutation': mutation,
                 'wire': wire, 'visible': marker in wire, 'executions': len(executions), 'pushes': len(pushes),
                 'part_state': state['state'], 'attempts': state['attempts'], 'task_raw_unchanged': before == after,
                 'messages_unchanged': messages_before == messages_after,
                 'conversation_unchanged': conversation.read_bytes() == conversation_before}
        print('FIRST_REPLY_LIFECYCLE ' + json.dumps(facts))
        assert (marker in wire) is visible, facts
        assert len(executions) == 2
        assert 'HEALTHY_NATIVE_SIBLING' in healthy_get
        assert all((marker in value) is visible for value in public.values()), public
        if not visible:
            assert before == after, facts
            assert messages_before == messages_after, facts
            assert conversation.read_bytes() == conversation_before, facts
            metrics_after = protocol.metrics.snapshot()
            metrics_after.pop('uptime_seconds')
            assert metrics_after == metrics_before
            assert pushes == pushes_before
            assert all(push[0] != identities['task'] for push in pushes)
            if method == 'SendMessage':
                rejected = json.loads(wire)
                assert 'error' in rejected and 'result' not in rejected, rejected
            else:
                assert ': done' in wire and 'TASK_STATE_COMPLETED' not in wire
                errors = [json.loads(line[6:]) for line in wire.splitlines() if line.startswith('data: ')]
                assert 'error' in errors[-1] and 'result' not in errors[-1], errors
        if policy == 'strict':
            assert before == after and not pushes, facts
            assert (state['state'], state['attempts']) == ('delivered', 1), facts
            if not visible:
                replay = await asyncio.to_thread(rpc, url, method, {'message': {
                    'messageId': 'target-input', 'contextId': 'target-context', 'parts': [{'text': 'once'}]}})
                assert marker not in replay
                assert len(executions) == 2 and not pushes
                still = next(v for v in pf.pending(db) if v['reply_to'] == identities['task'])['parts']['text']
                assert (still['state'], still['attempts']) == ('delivered', 1)
                assert conversation.read_bytes() == conversation_before
        if operation == 'input-required':
            assert 'TASK_STATE_INPUT_REQUIRED' in wire
    finally:
        release.set()
        if reader is not None:
            await asyncio.gather(reader, return_exceptions=True)
        if owner is not None:
            await owner.close()
        await adapter.disconnect()
        store.close_all_db_handles()
        db.close()
