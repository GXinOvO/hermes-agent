"""SIGKILL a real gateway after settlement and around the first exact send claim."""
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import threading

import pytest

from tests.gateway.fixtures.local_recovery_probe import child_env, daemon
from tests.waiting import wait_until


class Model(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        text = next((m.get('content', '') for m in reversed(body.get('messages', [])) if m['role']=='user'), '')
        self.server.requests.append(str(text))
        answer = 'ACK_' + str(text)
        if text == 'PROBE_MEDIA':
            answer = 'MEDIA:' + str(self.server.media)
        message = {'role':'assistant', 'content':answer}
        frame = {'id':'local', 'choices':[{'index':0, 'message':message, 'finish_reason':'stop'}],
                 'usage':{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2}}
        payload = json.dumps(frame).encode()
        kind = 'application/json'
        if body.get('stream'):
            frame['choices'][0]['delta'] = frame['choices'][0].pop('message')
            payload = ('data: '+json.dumps(frame)+'\n\ndata: [DONE]\n\n').encode()
            kind = 'text/event-stream'
        self.send_response(200)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def values(home):
    with closing(sqlite3.connect(f'file:{home / "state.db"}?mode=ro', uri=True)) as conn:
        return [json.loads(r[0]) for r in conn.execute(
            "SELECT value FROM state_meta WHERE key LIKE 'gateway.pending_final.v1:%'")]


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('kind', ['automation','human'])
@pytest.mark.parametrize('fault', ['before', 'claimed', 'during'])
@pytest.mark.parametrize('media', [False, True])
def test_fresh_gateway_consumes_only_unclaimed_final_without_new_inference(tmp_path, fault, media, kind):
    root = Path(__file__).resolve().parents[2]
    home, user = tmp_path / 'state', tmp_path / 'user'
    home.mkdir(mode=0o700)
    user.mkdir()
    media_path = home / 'sealed-report.txt'
    media_path.write_text('exact sealed media bytes')
    model = ThreadingHTTPServer(('127.0.0.1', 0), Model)
    model.requests, model.media = [], media_path
    thread = threading.Thread(target=model.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{model.server_port}/v1'
    (home / 'config.yaml').write_text(json.dumps({
        'gateway':{'multiplex_profiles':False}, 'model':{'provider':'custom','default':'probe','base_url':url},
        'streaming':{'enabled':False}, 'auxiliary':{'title_generation':{'enabled':False}},
        'platforms':{'slack':{'enabled':True, 'token':'owned-inert-token'}},
        'platform_toolsets':{'slack':[]}, 'terminal':{'cwd':str(home)}}))
    env = child_env()
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home), PYTHONPATH=str(root),
               OPENAI_API_KEY='loopback-only', OPENAI_BASE_URL=url, PYTHONUNBUFFERED='1',
               FINAL_PROBE_MODE=fault, FINAL_PROBE_MEDIA=str(int(media)), FINAL_PROBE_KIND=kind)
    marker = 'PROBE_MEDIA' if media else 'PROBE_TEXT'
    def target_wire():
        path = home / 'wire.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()
                if ('exact sealed media bytes' if media else marker) in json.loads(line)['content']] if path.exists() else []
    try:
        with daemon(root, home, env, barrier=True, fixture='pending_final_daemon.py') as (proc, desc):
            first_pid, first_epoch = proc.pid, desc['authority_epoch']
            (home / 'start-probe').touch()
            assert proc.wait(timeout=60) == -9, (home / 'first.log').read_text()
        before = target_wire()
        target = [v for v in values(home) if v['binding']['request_id']=='probe']
        assert len(target) == 1, {'saved':values(home), 'log':(home/'first.log').read_text()}
        part = target[0]['parts']['attachments' if media else 'text']
        assert part['state'] == ('pending' if fault=='before' else 'attempting')
        assert len(before) == int(fault=='during')
        env['FINAL_PROBE_MODE'] = 'recover'
        with daemon(root, home, env, barrier=True, fixture='pending_final_daemon.py') as (proc, desc):
            assert proc.pid != first_pid and desc['authority_epoch'] > first_epoch
            if fault == 'before':
                wait_until(lambda: len(target_wire())==1, timeout=20, what='sealed final delivered')
            after = target_wire()
            target = next(v for v in values(home) if v['binding']['request_id']=='probe')
            assert target['parts']['attachments' if media else 'text']['state'] == ('delivered' if fault=='before' else 'unknown')
            assert len(after) == (1 if fault=='before' else len(before))
            assert sum(marker in text for text in model.requests) == 1, model.requests
        with daemon(root, home, env, barrier=True, fixture='pending_final_daemon.py'):
            assert target_wire() == after
            assert sum(marker in text for text in model.requests) == 1
    finally:
        model.shutdown()
        model.server_close()
        thread.join(timeout=5)
