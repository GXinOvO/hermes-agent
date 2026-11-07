from contextlib import closing
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sqlite3
import threading
import urllib.error
import urllib.request

from tests.gateway.fixtures.local_recovery_probe import Model, child_env, daemon
from tests.gateway.test_native_telegram_startup_recovery import wait_for


def rpc(url, method, params):
    body = json.dumps({'jsonrpc': '2.0', 'id': 'startup07', 'method': method, 'params': params}).encode()
    req = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=45) as response:
        return json.loads(response.read())


def send(url, marker, message_id):
    return rpc(url, 'SendMessage', {'message': {'role': 'ROLE_USER', 'messageId': message_id,
        'contextId': 'public-default07-context', 'parts': [{'text': marker}]}})


def read_state(home):
    with closing(sqlite3.connect(f'file:{home / "state.db"}?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(row) for row in db.execute('SELECT * FROM session_admissions')]
        pending = {key: json.loads(raw) for key, raw in db.execute(
            'SELECT key,value FROM state_meta WHERE key LIKE ?', ('gateway.pending_final.v1:%',))}
    return rows, pending


def test_public_human_default_settlement_fresh_gateway_once(tmp_path):
    root = Path(__file__).resolve().parents[2]
    home, user = tmp_path / 'state', tmp_path / 'user'
    home.mkdir(mode=0o700)
    user.mkdir()
    model = ThreadingHTTPServer(('127.0.0.1', 0), Model)
    model.requests, model.blocked, model.release = [], threading.Event(), threading.Event()
    thread = threading.Thread(target=model.serve_forever, daemon=True)
    thread.start()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    base = f'http://127.0.0.1:{model.server_port}/v1'
    config = {'model': {'provider': 'custom', 'default': 'local-wire-stub', 'base_url': base},
        'gateway': {'multiplex_profiles': False},
        'platforms': {'a2a': {'enabled': True, 'extra': {'port': port}}},
        'auxiliary': {'title_generation': {'enabled': False}}, 'platform_toolsets': {'a2a': []},
        'terminal': {'cwd': str(home)}}
    (home / 'config.yaml').write_text(json.dumps(config))
    env = child_env()
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home), PYTHONPATH=str(root),
        OPENAI_API_KEY='loopback-only', OPENAI_BASE_URL=base, PYTHONUNBUFFERED='1',
        HERMES_DISABLE_LAZY_INSTALLS='1', A2A_PORT=str(port))
    url = f'http://127.0.0.1:{port}/'
    evidence = []

    def diagnostic():
        logs = [p.read_text() for p in (home / 'logs').glob('*.log')]
        return repr(read_state(home)) + '\n' + '\n'.join(logs) + (home / 'first.log').read_text()

    try:
        with daemon(root, home, env, barrier=True, fixture='default_public_a2a_crash.py') as (proc, desc):
            warm = send(url, 'WARM_HISTORY', 'public07-warm')
            assert 'result' in warm, (warm, diagnostic())
            try:
                send(url, 'DEFAULT_PUBLIC_07_SEALED', 'public07-input')
            except (OSError, urllib.error.URLError):
                pass
            assert proc.wait(timeout=60) == 23, diagnostic()
            before = json.loads((home / 'preclaim-exit.json').read_text())
            value = before['pending']
            aid = value['binding']['admission_id']
            task = value['reply_to']
            rows, _ = read_state(home)
            row = next(row for row in rows if row['admission_id'] == aid)
            payload = json.loads(row['payload_json'])
            assert row['status'] == 'terminal' and row['outcome'] == 'completed'
            assert row['principal_id'].startswith('messaging:')
            assert value['binding']['policy'] == 'human_reply_v1'
            assert 'automation' not in payload['native_text_v1'] and payload['native_text_v1']['ingress_sha256']
            assert value['parts']['text']['state'] == 'pending' and value['parts']['text']['attempts'] == 0
            evidence.append({'pid': proc.pid, 'epoch': desc['authority_epoch'], 'stage': 'settled_before_claim',
                             'admission': aid, 'task': task, 'policy': value['binding']['policy']})
        expected = 'DEFAULT_PUBLIC_07_SEALED'
        original_binding = value['binding']
        for _ in range(2):
            with daemon(root, home, env, barrier=False) as (proc, desc):
                def completed():
                    reply = rpc(url, 'GetTask', {'taskId': task, 'includeArtifacts': True})
                    return expected in json.dumps(reply) and 'TASK_STATE_COMPLETED' in json.dumps(reply)
                wait_for(completed, diagnostic, timeout=35)
                replay = send(url, expected, 'public07-input')
                assert expected in json.dumps(replay) and task in json.dumps(replay), replay
                rows, pending = read_state(home)
                current = pending['gateway.pending_final.v1:' + aid]
                assert current['binding'] == original_binding
                assert current['parts']['text']['state'] == 'delivered'
                assert current['parts']['text']['attempts'] == 1
                assert current['parts']['text']['delivered_at'] >= current['created_at']
                evidence.append({'pid': proc.pid, 'epoch': desc['authority_epoch'], 'stage': 'ordinary_gateway_restart',
                                 'admission': aid, 'task': task, 'part': current['parts']['text']})
        prompts = [next((m.get('content', '') for m in reversed(body['messages']) if m['role'] == 'user'), '')
                   for body in model.requests]
        calls = sum(expected in str(prompt) for prompt in prompts)
        assert calls == 1, prompts
        assert len({entry['pid'] for entry in evidence}) == 3
        assert evidence[0]['epoch'] < evidence[1]['epoch'] < evidence[2]['epoch']
        print('DEFAULT_PUBLIC_07 ' + json.dumps({'boots': evidence, 'model_executions_for_result': calls,
                                               'total_model_calls': len(model.requests), 'HUMAN_preserved': True}))
    finally:
        model.release.set()
        model.shutdown()
        model.server_close()
        thread.join(timeout=5)
