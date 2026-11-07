"""Transport crash receipts: recovery must not replay a send lacking an ACK."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


_PEER = r'''
import asyncio, json, os, sqlite3, sys
from pathlib import Path
from gateway import delivery_ledger as dl
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource

home, mode = Path(sys.argv[1]), sys.argv[2]
dl._db_path = lambda: home / 'state.db'
wire = home / 'wire.jsonl'
class Adapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True), Platform.SLACK)
    async def connect(self, **kw): return True
    async def disconnect(self): pass
    async def get_chat_info(self, chat_id): return {}
    async def send(self, chat_id, content, **kw):
        with wire.open('a') as f:
            f.write(json.dumps({'pid':os.getpid(), 'content':content})+'\n')
            f.flush(); os.fsync(f.fileno())
        if mode == 'crash': os._exit(73)
        return SendResult(success=True, message_id='ack')

if mode == 'crash':
    # An exact pre-upgrade row: no policy/provenance. Sending may have split on the wire.
    c = sqlite3.connect(home / 'state.db')
    c.execute("""CREATE TABLE delivery_obligations (
      obligation_id TEXT PRIMARY KEY, session_key TEXT NOT NULL, platform TEXT NOT NULL,
      chat_id TEXT NOT NULL, thread_id TEXT, content TEXT NOT NULL, state TEXT NOT NULL,
      attempts INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL,
      owner_pid INTEGER, owner_started_at INTEGER, last_error TEXT, adapter_profile TEXT)""")
    import time
    c.execute('INSERT INTO delivery_obligations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
              ('legacy', 'session', 'slack', 'chat', None, 'complete answer', 'attempting',
               0, time.time(), time.time(), os.getpid(), dl._start_time(os.getpid()), None, 'default'))
    c.commit(); c.close()
    asyncio.run(Adapter().send('chat', 'partial first chunk'))
else:
    rows = dl.sweep_recoverable()
    async def replay():
        a = Adapter()
        for row in rows:
            await a.send(row['chat_id'], row['content'])
            dl.mark_delivered(row['obligation_id'])
    asyncio.run(replay())
    with dl._connect() as c:
        states = dict(c.execute('SELECT obligation_id,state FROM delivery_obligations'))
    print(json.dumps({'recovered':len(rows),'states':states}))
'''


def _peer(tmp_path, mode):
    env = dict(os.environ, HERMES_HOME=str(tmp_path))
    return subprocess.run([sys.executable, '-c', _PEER, str(tmp_path), mode],
                          env=env, capture_output=True, text=True, timeout=45, check=False)


def test_new_process_never_replays_partial_legacy_send_without_provenance(tmp_path):
    crash = _peer(tmp_path, 'crash')
    assert crash.returncode == 73, crash.stdout + crash.stderr
    before = (tmp_path / 'wire.jsonl').read_text().splitlines()
    assert len(before) == 1
    recovered = _peer(tmp_path, 'recover')
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    receipt = json.loads(recovered.stdout.splitlines()[-1])
    after = (tmp_path / 'wire.jsonl').read_text().splitlines()
    assert after == before, {'receipt':receipt, 'wire':after}
    assert receipt == {'recovered':0, 'states':{'legacy':'unknown'}}


def _strict_peer(tmp_path, mode):
    peer = Path(__file__).parent / 'fixtures' / 'strict_final_peer.py'
    env = dict(os.environ, HERMES_HOME=str(tmp_path))
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[2])
    return subprocess.run([sys.executable, str(peer), str(tmp_path), mode], env=env,
                          capture_output=True, text=True, timeout=45, check=False)


@pytest.mark.parametrize('mode', ['strict-crash', 'claimed-crash', 'attachment-crash'])
def test_strict_orphan_send_has_no_second_attempt_in_new_process(tmp_path, mode):
    crash = _strict_peer(tmp_path, mode)
    assert crash.returncode == 73, crash.stdout + crash.stderr
    wire = tmp_path / 'wire.jsonl'
    before = wire.read_text() if wire.exists() else ''
    recovered = _strict_peer(tmp_path, 'recover')
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    receipt = json.loads(recovered.stdout.splitlines()[-1])
    assert (wire.read_text() if wire.exists() else '') == before
    assert receipt['recovered'] == 0
    assert len(receipt['states']) == 1
    state, attempts, policy, provenance, kind = receipt['states'][0]
    assert (state, attempts, policy) == ('unknown', 1, 'strict_automation_v1')
    binding = json.loads(provenance)
    assert binding['profile_home'] == str(tmp_path.resolve()) and binding['session_id'] == 'sid'
    assert binding['generation'] == 1 and binding['admission_id']
    assert binding['consume_only'] is True
    assert kind == ('attachments' if mode == 'attachment-crash' else 'text')


@pytest.mark.parametrize('mode', ['partial-failure', 'attachment-failure', 'claim-storage-failure',
                                 'ack-storage-failure', 'unsettled-result'])
def test_strict_fail_closed_and_never_retries_same_final(tmp_path, mode):
    result = _strict_peer(tmp_path, mode)
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads(result.stdout.splitlines()[-1])
    assert receipt['success'] == [False, False], receipt
    wire = tmp_path / 'wire.jsonl'
    if mode in {'claim-storage-failure', 'unsettled-result'}:
        assert not wire.exists()
    else:
        assert len(wire.read_text().splitlines()) == 1, receipt


@pytest.mark.parametrize('transport', ['edit', 'draft', 'auto'])
def test_strict_stream_segments_finalize_only_after_durable_result(tmp_path, transport):
    result = _strict_peer(tmp_path, 'stream-' + transport)
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads(result.stdout.splitlines()[-1])
    assert receipt['success'] == [True, True] and receipt['edits'] == 0, receipt
    assert receipt['states'][0][0] == 'delivered'
    assert len((tmp_path / 'wire.jsonl').read_text().splitlines()) == 1
