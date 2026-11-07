"""Real producer/recovery process with a loopback A2A JSON-RPC reader."""
import asyncio
import json
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import urllib.request

from gateway import pending_final as pf
from gateway.config import GatewayConfig, PlatformConfig
from gateway.session import SessionStore
from gateway.session_authorities import SessionAuthorities
from gateway.session_delivery_policy import automation_delivery_scope
from gateway.session_results import finish_result
from hermes_state import SessionDB
from hermes_state_runtime import admit_session_input, begin_runtime_epoch, claim_session_input
from plugins.platforms.a2a.adapter import A2AAdapter


def rpc(url, method, params):
    raw = json.dumps({'jsonrpc': '2.0', 'id': 'reader', 'method': method, 'params': params}).encode()
    request = urllib.request.Request(url, data=raw, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.read().decode()


async def main():
    home, mode = Path(sys.argv[1]), sys.argv[2]
    adapter = A2AAdapter(PlatformConfig(enabled=True, extra={'port': 0, 'agents': {'other': {'local': True, 'tenant': 'other'}}}))
    if mode in {'seal', 'seal-legacy'}:
        async def produce(event):
            counter = home / 'executions'
            counter.write_text(str(int(counter.read_text()) + 1) if counter.exists() else '1')
            db = SessionDB(home / 'state.db')
            db.create_session('sid', source='a2a')
            epoch = begin_runtime_epoch(db, instance_id='producer')
            route = 'agent:main:a2a:dm:' + event.source.chat_id
            envelope = {'route': route, 'source': {**event.source.to_dict(), 'is_bot': False},
                        'event': {}, 'timestamp': '1970-01-01T00:00:00+00:00',
                        'automation': {'owner': 'sid', 'identity': event.message_id, 'result_recovery_v1': 'consume_only'}}
            if mode == 'seal':
                envelope['ingress_sha256'] = event._ingress_sha256
            admit_session_input(db, epoch=epoch, principal_id='automation:' + route, session_id='sid',
                                request_id=event.message_id, payload={'text': event.text, 'native_text_v1': envelope})
            row = claim_session_input(db, epoch=epoch, session_id='sid')
            authority = SimpleNamespace(db=db, runner=SimpleNamespace(_adapter_for_source=lambda source: adapter))
            async with automation_delivery_scope(authority, row, event):
                prepared = await pf.prepare(authority, row, event, route, 'sealed native answer')
            finish_result(db, epoch=epoch, row=row, response='sealed native answer', outcome='completed',
                          result={'result': {'final_response': 'sealed native answer', 'messages': [], '_pending_final_v1': prepared}, 'usage': {}})
            (home / 'producer.json').write_text(json.dumps({'task': event.message_id, 'context': event.source.chat_id}))
            db.close()
            os._exit(23)  # real crash after settlement, before the first delivery claim
        adapter._message_handler = produce
        assert await adapter.connect()
        print(json.dumps({'url': f'http://127.0.0.1:{adapter._httpd.server_port}/'}), flush=True)
        await asyncio.Event().wait()
        return
    db = SessionDB(home / 'state.db')
    value = pf.pending(db)[0]
    route = value['binding']['route']
    entry = SimpleNamespace(session_id='sid', suspended=False, session_key=route)
    store = SessionStore(sessions_dir=home / 'sessions', config=GatewayConfig())
    store.lookup_by_session_key = lambda key: entry if key == route else None
    authority = SimpleNamespace(db=db, profile_id=str(home), logical_owner=lambda sid: sid)
    registry = SessionAuthorities(home)
    registry.add(home, authority)
    runner = SimpleNamespace(session_authorities=registry, session_authority=authority, session_store=store,
                             config=SimpleNamespace(multiplex_profiles=False),
                             _adapter_for_source=lambda source: adapter, _delivery_adapter_for=lambda source: adapter)
    authority.runner = runner
    adapter.authorize_source = lambda source: True
    watcher = None
    if mode == 'watching':
        ready = threading.Event()
        original_watch = adapter.tasks.watch
        def watch(*args):
            future = original_watch(*args)
            ready.set()
            return future
        adapter.tasks.watch = watch
        assert await adapter.connect()
        watcher = asyncio.create_task(asyncio.to_thread(rpc, f'http://127.0.0.1:{adapter._httpd.server_port}/',
                                                       'SubscribeToTask', {'taskId': value['reply_to']}))
        assert await asyncio.to_thread(ready.wait, 10)
    if mode in {'wrong', 'wrong-payload', 'wrong-task', 'wrong-metadata', 'unclaimed'}:
        from gateway.strict_transport import strict_transport_scope
        chat = 'other-context' if mode == 'wrong' else 'real-context'
        content = 'changed result' if mode == 'wrong-payload' else 'sealed native answer'
        task = 'other-task' if mode == 'wrong-task' else value['reply_to']
        metadata = {'notify': False} if mode == 'wrong-metadata' else value['metadata']
        with strict_transport_scope():
            result = await adapter.send(chat, content, reply_to=task, metadata=metadata)
        print(json.dumps({'success': result.success, 'part': pf.pending(db)[0]['parts']['text']}), flush=True)
        db.close()
        return
    if mode == 'resend':
        async def forbidden(event):
            raise AssertionError('completed producer executed again')
        adapter._message_handler = forbidden
        assert await adapter.connect()
        url = f'http://127.0.0.1:{adapter._httpd.server_port}/'
        response = await asyncio.to_thread(rpc, url, 'SendMessage', {'message': {
            'role': 'ROLE_USER', 'messageId': 'one-input', 'contextId': 'real-context',
            'parts': [{'text': 'do the work once'}]}})
        print(json.dumps({'response': response, 'executions': (home / 'executions').read_text()}), flush=True)
        await adapter.disconnect()
        db.close()
        return
    if mode == 'lost':
        # Commit the real publication then lose the adapter ACK before ledger finish.
        from gateway.strict_transport import single_attempt_transport
        original = adapter.send
        @single_attempt_transport
        async def lost(*args, **kwargs):
            result = await original(*args, **kwargs)
            assert result.success, result.error
            raise ConnectionError('accepted publication; adapter ACK lost')
        adapter.send = lost
    delivered = await pf.recover(runner)
    if watcher is not None:
        watched = await watcher
        assert 'sealed native answer' in watched, watched
        await adapter.disconnect()
    assert await adapter.connect()
    url = f'http://127.0.0.1:{adapter._httpd.server_port}/'
    task = value['reply_to']
    responses = {}
    for method in ['GetTask', 'ListTasks', 'SubscribeToTask']:
        responses[method] = await asyncio.to_thread(rpc, url, method, {'taskId': task, 'includeArtifacts': True})
    other = await asyncio.to_thread(rpc, url + 'other/', 'GetTask', {'taskId': task})
    print(json.dumps({'delivered': delivered, 'part': pf.pending(db)[0]['parts']['text'],
                      'public': responses, 'other': other, 'executions': (home / 'executions').read_text()}), flush=True)
    await asyncio.to_thread(lambda: None)
    await adapter.disconnect()
    db.close()


if __name__ == '__main__':
    asyncio.run(main())
