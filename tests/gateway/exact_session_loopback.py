import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


async def main():
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway.session_authority import SessionAuthority
    from gateway.session_controls import AuthorityConnection
    from gateway.session_local import create_local_session
    import hermes_state_runtime as rt

    with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as directory:
        home = Path(directory)
        os.environ['HERMES_HOME'] = str(home)
        os.chdir(home)
        store = SessionStore(home / 'sessions', GatewayConfig())
        db = store._db
        runner = SimpleNamespace(session_store=store, _session_db=db, adapters={}, _draining=False,
                                 _evict_cached_agent=lambda route: None, _cached_agent_for=lambda route: None,
                                 _handle_message=lambda event: 'unused',
                                 _resolve_session_agent_runtime=lambda **kwargs: ('frozen', {}))
        runner._adapter_for_source = lambda source: runner.adapters.get(source.platform)
        authority = SessionAuthority(runner, profile_id=str(home), instance_id='loopback-owner', db=db,
                                     epoch=rt.begin_runtime_epoch(db, instance_id='loopback-owner'))
        connection = AuthorityConnection(authority, object(), {'user_id': 'human'})
        ref = create_local_session(authority, connection.actor,
                                   dict(request_id='source', source='gui', cwd=str(home), model='frozen', toolsets=[]))
        db.append_message(ref.session_id, role='user', content='Source transcript must remain visible')
        db.append_message(ref.session_id, role='assistant', content='', tool_calls=[
            {'id': 'native-review-todo', 'type': 'function', 'function': {'name': 'todo_list', 'arguments': '{}'}}])
        db.append_message(ref.session_id, role='tool', content=json.dumps({'revision': 3, 'todos': [
            {'id': 'ship', 'content': 'Check remote adoption', 'status': 'pending'}]}),
            tool_name='todo_list', tool_call_id='native-review-todo')
        db.append_message(ref.session_id, role='assistant', content='::task{title="Source review" remaining="Deploy" '
                          'deferred="Mobile" tests="passed" tests_evidence="source-receipt.json"}')
        db.append_message(ref.session_id, role='user', content='Current requirement must remain separate')
        print(json.dumps({'session_id': ref.session_id}), flush=True)
        while line := await asyncio.to_thread(sys.stdin.readline):
            request = json.loads(line)
            if request['method'] == 'client.capabilities':
                reply = {'jsonrpc': '2.0', 'id': request['id'], 'result': {'server_requests': []}}
            else:
                request['params'].pop('profile', None)
                reply = await connection.dispatch(request)
            print(json.dumps(reply), flush=True)
        db.close()


if __name__ == '__main__':
    asyncio.run(main())
