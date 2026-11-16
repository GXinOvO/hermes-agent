import asyncio
from contextlib import nullcontext
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from typing import cast

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


async def main():
    from gateway.config import GatewayConfig, Platform
    from gateway.session import SessionSource, SessionStore
    from gateway.session_authority import SessionAuthority
    from gateway.session_controls import AuthorityConnection
    from gateway.session_local import create_local_session
    from hermes_state import SessionDB
    from hermes_state_runtime import begin_runtime_epoch

    seed = sys.argv[1] == '--seed'
    mode = sys.argv[3] if seed else sys.argv[1]
    model = 'mock-model' if seed else 'frozen'
    route = {'gateway_runtime': {'provider': 'custom', 'base_url': sys.argv[4] + '/v1', 'api_mode': 'openai'}} if seed else None
    text = sys.argv[5] if seed else 'Original source requirement'
    with nullcontext(sys.argv[2]) if seed else tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as directory:
        home = Path(directory)
        os.environ['HERMES_HOME'] = str(home)
        os.chdir(home)
        store = SessionStore(home / 'sessions', GatewayConfig())
        db = cast(SessionDB, store._db)
        runner = SimpleNamespace(session_store=store, _session_db=db, adapters={}, _draining=False,
                                 _evict_cached_agent=lambda route: None, _cached_agent_for=lambda route: None,
                                 _handle_message=lambda event: 'unused',
                                 _resolve_session_agent_runtime=lambda **kwargs: ('frozen', {}),
                                 _channel_override_for=lambda source: None,
                                 _session_model_override=lambda route: None,
                                 _resolve_session_reasoning_config=lambda **kwargs: None,
                                 _resolve_session_service_tier=lambda **kwargs: None)
        runner._adapter_for_source = lambda source: runner.adapters.get(source.platform)
        runner._restored_source = lambda entry: entry.origin
        authority = SessionAuthority(runner, profile_id=str(home), instance_id='compressed-owner', db=db,
                                     epoch=begin_runtime_epoch(db, instance_id='compressed-owner'))
        connection = AuthorityConnection(authority, object(), {'user_id': 'human', **({'provider': 'session-token'} if seed else {})})
        if mode == 'local':
            ref = create_local_session(authority, connection.actor,
                                       dict(request_id='source', source='gui', cwd=str(home), model=model, toolsets=[]))
            source = authority.sessions[ref.session_id].source
        else:
            source = SessionSource(platform=Platform.TELEGRAM, chat_id='4242', user_id='u-4242', chat_type='dm')
            ref = authority.register(source)
        live = authority.sessions[ref.session_id]
        db.update_session_model(ref.session_id, model)
        if route:
            db.patch_session_model_config(ref.session_id, route)
        db.append_message(ref.session_id, role='user', content=text)
        assert db.try_acquire_compression_lock(ref.session_id, 'task-navigation')
        try:
            db.publish_compression_child(parent_session_id=ref.session_id, child_session_id='compressed-tip',
                                         source='gui' if mode == 'local' else 'telegram',
                                         model=model, model_config=route,
                                         messages=[{'role': 'user', 'content': 'Continue the original source requirement'}],
                                         compression_lock_holder='task-navigation')
        finally:
            db.release_compression_lock(ref.session_id, 'task-navigation')
        if mode != 'local':
            assert store.advance_compression_session(live.route, ref.session_id, 'compressed-tip') is not None
            db.record_gateway_session_peer('compressed-tip', source='telegram', user_id=source.user_id,
                                           session_key=live.route, chat_id=source.chat_id, chat_type='dm',
                                           origin_json=json.dumps(source.to_dict()))
        print(json.dumps({'session_id': ref.session_id, 'tip': 'compressed-tip', 'owner': {
            'instance_id': authority.instance_id, 'profile_id': authority.profile_id,
            'authority_epoch': authority.epoch, 'store_instance_id': db.get_meta('store_instance_id')}}), flush=True)
        if seed:
            await connection.close()
            db.close()
            return
        while line := await asyncio.to_thread(sys.stdin.readline):
            request = json.loads(line)
            if request['method'] == 'client.capabilities':
                reply = {'jsonrpc': '2.0', 'id': request['id'], 'result': {'server_requests': []}}
            else:
                request['params'].pop('profile', None)
                reply = await connection.dispatch(request)
            print(json.dumps(reply), flush=True)
        await connection.close()
        db.close()


if __name__ == '__main__':
    asyncio.run(main())
