import asyncio
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import threading

import pytest

from tests.gateway.fixtures.local_recovery_probe import child_env, daemon, rpc, websocket
from tests.gateway.test_session_busy_controls import CorrectionModel
from tests.gateway.test_session_tui_side import call_and_await_event


@pytest.mark.platforms('linux')
def test_new_gateway_reads_sealed_side_result_without_another_model_call(tmp_path):
    root = Path(__file__).resolve().parents[2]
    home, user = tmp_path / 'state', tmp_path / 'user'
    home.mkdir(mode=0o700)
    user.mkdir()
    peer = ThreadingHTTPServer(('127.0.0.1', 0), CorrectionModel)
    peer.requests = []
    peer.blocked, peer.release = threading.Event(), threading.Event()
    thread = threading.Thread(target=peer.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{peer.server_port}/v1'
    (home / 'config.yaml').write_text(json.dumps({
        'gateway':{'multiplex_profiles':False},
        'model':{'provider':'custom', 'default':'side-result-fixture', 'base_url':url},
        'auxiliary':{'title_generation':{'enabled':False}}, 'model_catalog':{'enabled':False},
        'models_dev':{'url':url.removesuffix('/v1') + '/models-dev-offline'}}))
    env = child_env()
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home), PYTHONPATH=str(root),
               OPENAI_API_KEY='loopback-only', OPENAI_BASE_URL=url, PYTHONUNBUFFERED='1')
    saved = {}
    try:
        with daemon(root, home, env, barrier=False) as (first, desc):
            async def produce():
                async with websocket(home, desc) as ws:
                    created = await rpc(ws, 'session.create', request_id='side-restart', source='tui', toolsets=[])
                    assert 'result' in created, (created, (home / 'restart.log').read_text()[-12000:])
                    sid = created['result']['session_id']
                    reply, event = await call_and_await_event(ws, 'prompt.background', 'background.complete',
                                                              session_id=sid, text='READ_ONLY_SIDE')
                    saved.update(sid=sid, payload=event['payload'], task=reply['result']['task_id'])
                    snapshot = (await rpc(ws, 'session.resume', session_id=sid))['result']
                    assert snapshot['background_results'] == [{'event_type':'background.complete', 'payload':saved['payload']}]
                    assert not snapshot['messages']
                    running = (await rpc(ws, 'prompt.background', session_id=sid, text='BLOCK_REDIRECT'))['result']
                    saved['running'] = running['task_id']
                    assert await asyncio.to_thread(peer.blocked.wait, 20)
            asyncio.run(produce())
            calls = len(peer.requests)
            assert calls > 0
            first.kill()
            first.wait(timeout=10)
            peer.release.set()
        with daemon(root, home, env, barrier=False) as (second, desc):
            assert first.pid != second.pid
            async def consume():
                async with websocket(home, desc) as ws:
                    snapshot = (await rpc(ws, 'session.resume', session_id=saved['sid']))['result']
                    items = snapshot['background_results']
                    assert items[0] == {'event_type':'background.complete', 'payload':saved['payload']}
                    assert items[1]['payload']['task_id'] == saved['running']
                    assert items[1]['payload']['text'].startswith('error: unknown_execution')
                    assert not snapshot['messages']
                    assert (await rpc(ws, 'session.resume', session_id=saved['sid']))['result']['background_results'] == snapshot['background_results']
                    foreign = (await rpc(ws, 'session.create', request_id='other-session', source='tui',
                                         toolsets=[]))['result']
                    assert not (await rpc(ws, 'session.resume', session_id=foreign['session_id']))['result']['background_results']
            asyncio.run(consume())
            assert len(peer.requests) == calls
    finally:
        peer.release.set()
        peer.shutdown()
        peer.server_close()
        thread.join(timeout=5)
