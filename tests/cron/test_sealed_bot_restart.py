"""Native scheduler, authenticated loopback door and actual provider consumer across process death."""
import asyncio
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import threading

import pytest

from tests.gateway.fixtures.local_recovery_probe import Model, child_env, daemon, rpc, websocket
from tests.waiting import async_wait_until


def _rows(home):
    with sqlite3.connect((home / 'state.db').as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute("SELECT * FROM session_admissions WHERE request_id LIKE 'bot:%'")]


class _RecordingModel(Model):
    def do_POST(self):
        self.server.http_paths.append(self.path)
        return super().do_POST()


class _BlockingModel(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.server.http_paths.append(self.path)
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.raw_requests.append({'path':self.path, 'has_messages':bool(body.get('messages')),
                                         'model':body.get('model', body.get('name'))})
        if not body.get('messages'):
            payload = b'{}'
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.server.requests.append(body)
        self.server.blocked.set()
        self.server.release.wait(50)
        self.send_response(503)
        self.end_headers()


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('consumer_started', [False, True])
def test_native_sealed_result_exact_admission_and_reply_survive_fresh_owner(tmp_path, monkeypatch, consumer_started, record_property):
    from cron import jobs, scheduler
    from cron.scheduler_provider import _profile_cron_scope
    from tools.bot_live_delivery import authority_delivery
    root = Path(__file__).resolve().parents[2]
    home, user = tmp_path / 'state', tmp_path / 'user'
    (home / 'scripts').mkdir(parents=True, mode=0o700)
    user.mkdir()
    script = home / 'scripts' / 'report.py'
    script.write_text('from pathlib import Path\np=Path(__file__).parents[1]/"producer-calls"\n'
                      'with p.open("a") as f: f.write("executed\\n")\nprint("EXACT_SEALED_REPORT")\n')
    peer = ThreadingHTTPServer(('127.0.0.1', 0), _BlockingModel if consumer_started else _RecordingModel)
    peer.requests = []
    peer.raw_requests, peer.http_paths = [], []
    peer.blocked, peer.release = threading.Event(), threading.Event()
    thread = threading.Thread(target=peer.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{peer.server_port}/v1'
    (home / 'config.yaml').write_text(json.dumps({
        'gateway':{'multiplex_profiles':False},
        'model':{'provider':'custom', 'default':'sealed-consumer-fixture', 'base_url':url},
        'auxiliary':{'title_generation':{'enabled':False}},
        'model_catalog':{'enabled':False}, 'platform_toolsets':{'cli':[]},
        'terminal':{'cwd':str(home)},
        'models_dev':{'url':url.removesuffix('/v1') + '/models-dev-offline'}}))
    env = child_env()
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home), PYTHONPATH=str(root),
               OPENAI_API_KEY='loopback-only', OPENAI_BASE_URL=url, PYTHONUNBUFFERED='1')
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(Path, 'home', lambda: user)
    saved, pids = {}, []
    try:
        with _profile_cron_scope(home):
            job = jobs.create_job(prompt=None, schedule='every 1h', script='report.py', no_agent=True, deliver='bot-chat')
            assert scheduler._run_one_job_body(job)
        with sqlite3.connect(home / 'cron' / 'deliveries.db') as db:
            native = db.execute('SELECT execution_id,job_json,content FROM deliveries').fetchone()
        frozen = json.loads(native[1])['_sealed_bot_result_v1']
        with daemon(root, home, env, barrier=False) as (first, desc):
            pids.append(first.pid)
            with _profile_cron_scope(home):
                scheduler.drain_delivery_queue(None, None)
            async def observe():
                expected_status = 'started' if consumer_started else 'terminal'
                await async_wait_until(lambda: len(_rows(home)) == 1 and _rows(home)[0]['status'] == expected_status,
                                       timeout=60, what='real sealed Bot consumer terminal')
                row = _rows(home)[0]
                descriptor = json.loads(row['payload_json'])['local_automation_v1']
                assert descriptor['result_recovery_v1'] == 'consume_only'
                assert descriptor['sealed_cron_result_v1']['seal'] == frozen['seal']
                sid = row['target_session_id']
                if not consumer_started:
                    async with websocket(home, desc) as ws:
                        history = await rpc(ws, 'session.resume', session_id=sid)
                        assert 'RECOVERY_ACK_' in json.dumps(history), history
                else:
                    assert await asyncio.to_thread(peer.blocked.wait, 20)
                saved.update(row=row, ref=descriptor['sealed_cron_result_v1'], sid=sid)
            asyncio.run(observe())
            payload = json.loads(saved['row']['payload_json'])
            params = dict(id=saved['row']['request_id'][4:], profile='default', message=payload['text'],
                          sealed_cron_result_v1=saved['ref'])
            if saved['ref']['target_session_id']:
                params['session_id'] = saved['ref']['target_session_id']
            first_receipt = authority_delivery(home, params)
            assert first_receipt['admission_id'] == saved['row']['admission_id']
            assert first_receipt['status'] == ('claimed' if consumer_started else 'settled'), first_receipt
            # The exact admitted reference remains a receipt read even after source retention.
            script.write_text('raise AssertionError("completed producer must never run again")\n')
            before_calls = len(peer.requests)
            assert before_calls == 1, peer.requests
            assert any('EXACT_SEALED_REPORT' in json.dumps(m.get('content'))
                       for m in peer.requests[0]['messages'] if m['role'] == 'user'), peer.requests
            first.kill()
            first.wait(timeout=10)
            peer.release.set()
        with daemon(root, home, env, barrier=False) as (second, desc):
            pids.append(second.pid)
            receipt = authority_delivery(home, params)
            assert receipt['admission_id'] == first_receipt['admission_id']
            assert receipt['status'] == ('ambiguous' if consumer_started else 'settled')
            if not consumer_started:
                assert receipt['reply'] == first_receipt['reply']
            with _profile_cron_scope(home):
                assert scheduler.drain_delivery_queue(None, None) == 0
            assert len(_rows(home)) == 1
            assert len(peer.requests) == before_calls
            assert pids[0] != pids[1]
            assert (home / 'producer-calls').read_text().splitlines() == ['executed']
            # Caller assertions cannot turn an ordinary payload into a sealed admission.
            async def forged():
                async with websocket(home, desc) as ws:
                    rejected = await rpc(ws, 'bot_relay.deliver', **{**params, 'id':'f'*64,
                        'sealed_cron_result_v1':{**params['sealed_cron_result_v1'], 'source_profile':'../../outside'}})
                    assert rejected['error']['message'] == 'admission_conflict', rejected
            asyncio.run(forged())
            assert len(_rows(home)) == 1 and len(peer.requests) == before_calls
            record_property('semantic_evidence', json.dumps({
                'pids':pids, 'consumer_started':consumer_started, 'inference_calls':len(peer.requests),
                'http_paths':peer.http_paths, 'http_requests':peer.raw_requests,
                'producer_calls':(home / 'producer-calls').read_text().splitlines(),
                'admission_count':len(_rows(home)), 'receipt_status':receipt['status'],
                'admission_id':receipt['admission_id'], 'exact_seal':frozen['seal'],
            }))
    finally:
        peer.release.set()
        peer.shutdown()
        peer.server_close()
        thread.join(timeout=5)
