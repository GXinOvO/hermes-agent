"""A damaged abandoned delivery cannot stop a healthy native queue drain."""
import asyncio
import json
import sqlite3

import pytest

from tests.cron import test_sealed_bot_consumer as bot_cases
from tests.cron.test_completed_result_recovery import _python
from tests.gateway.test_session_bot_retry import _admissions, _settle_quietly

pytest_plugins = ['tests.cron.test_sealed_bot_consumer']


def _abandoned(bot, monkeypatch, damage):
    from cron import delivery_queue as queue
    job, row = bot_cases._queued_job(bot)
    for key in ('dead-before', 'damaged', 'dead-after', 'live-damaged'):
        queue.enqueue(key, job, row['content'])
    child = _python(bot.home, '''
        from cron import delivery_queue as queue
        for key in ('dead-before', 'damaged', 'dead-after', 'live-damaged'):
            assert queue.claim_next(execution_id=key)
        ''')
    assert child.returncode == 0, (child.stdout, child.stderr)
    column, value = damage
    with sqlite3.connect(bot.home / 'cron' / 'deliveries.db') as db:
        db.execute(f'UPDATE deliveries SET {column}=? WHERE execution_id=?', (value, 'damaged'))
        # A malformed owner must not make a live delivery look reclaimable.
        import os
        db.execute('UPDATE deliveries SET owner_pid=?, job_json=? WHERE execution_id=?',
                   (os.getpid(), '{', 'live-damaged'))
    return row['execution_id'], queue.get_status('damaged'), queue.get_status('live-damaged')


def test_live_foreign_owner_without_fingerprint_is_not_reclaimed(tmp_path, monkeypatch):
    import os
    import subprocess
    import sys
    from tests.waiting import wait_until
    from cron import delivery_queue as queue
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    queue.enqueue('live', {'id': 'valid-job'}, 'owned result')
    code = '''
from pathlib import Path
import sys
from cron import delivery_queue as queue
assert queue.claim_next(execution_id='live')
with queue._transaction() as db:
    db.execute("UPDATE deliveries SET owner_started_at=NULL WHERE execution_id='live'")
Path(sys.argv[1]).write_text('ready')
sys.stdin.read()
'''
    ready = tmp_path / 'owner-ready'
    child = subprocess.Popen([sys.executable, '-c', code, str(ready)], env=dict(os.environ),
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        wait_until(ready.exists, timeout=10, what='live queue owner')
        before = queue.get_status('live')
        assert queue.recover_abandoned() == 0
        assert queue.get_status('live') == before
        assert queue.drain(lambda *a: pytest.fail('live owner must retain delivery')) == 0
    finally:
        child.communicate(timeout=10)
        assert child.returncode == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', [
    ('job_json', '{'), ('job_json', '[]'), ('owner_pid', 'bad'),
    ('owner_pid', 0), ('owner_started_at', 'bad'),
])
async def test_bad_abandoned_row_preserves_evidence_and_healthy_bot_drains_once(sealed_bot, monkeypatch, damage):
    from cron import delivery_queue as queue, scheduler
    bot = sealed_bot
    payloads = bot_cases._wire(bot, monkeypatch)
    await asyncio.to_thread(bot_cases._produce, bot)
    execution_id, damaged, live = await asyncio.to_thread(_abandoned, bot, monkeypatch, damage)
    monkeypatch.setattr(queue, 'MAX_TERMINAL_DELIVERIES', 0)
    assert await asyncio.to_thread(scheduler.drain_delivery_queue, None, None) == 1
    await _settle_quietly(bot)
    assert len(payloads) == len(_admissions(bot)) == 1
    assert _admissions(bot)[0]['payload']['local_automation_v1']['result_recovery_v1'] == 'consume_only'
    status = queue.get_status(execution_id)['status']
    if status == 'pending':
        assert await asyncio.to_thread(scheduler.drain_delivery_queue, None, None) == 1
    else:
        assert status == 'delivered'
    assert queue.get_status(execution_id)['status'] == 'delivered'
    for key in ('dead-before', 'dead-after'):
        assert queue.get_status(key)['status'] == 'unknown'
        assert queue.enqueue(key, {}, 'do not resend')['status'] == 'unknown'
    assert queue.get_status('damaged') == damaged
    assert queue.get_status('live-damaged') == live
    assert await asyncio.to_thread(scheduler.drain_delivery_queue, None, None) == 0
    assert (bot.home / 'producer-calls').read_text().splitlines() == ['executed']
    with sqlite3.connect(bot.home / 'cron' / 'deliveries.db') as db:
        assert set(db.execute('SELECT execution_id, terminal_status FROM delivery_tombstones')) == {
            (execution_id, 'delivered'), ('dead-before', 'unknown'), ('dead-after', 'unknown')}
        assert set(db.execute('SELECT execution_id FROM deliveries')) == {('damaged',), ('live-damaged',)}
    print(json.dumps({'damage': damage, 'consumed': execution_id, 'model_replay': 0,
                      'script_calls': 1, 'damaged_row_preserved': True, 'unknown_tombstones': 2}))
