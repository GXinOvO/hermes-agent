"""Cron Bot Chat admission deferral must remain pending until the owner settles it."""
import asyncio

import pytest

from cron import delivery_queue as queue, executions, scheduler
from tests.cron.test_sealed_bot_consumer import _produce, _wire
from tests.gateway.test_session_bot_retry import _admissions, _settle_quietly

pytest_plugins = ['tests.cron.test_sealed_bot_consumer']


@pytest.mark.asyncio
async def test_single_bot_admission_is_pending_then_settled_without_redelivery(sealed_bot, monkeypatch):
    from gateway import session_finite
    from cron import jobs

    bot = sealed_bot
    started = asyncio.Event()
    release = asyncio.Event()
    original = session_finite.execute_finite_admission

    async def gated(authority, ref, row):
        started.set()
        await release.wait()
        return await original(authority, ref, row)

    monkeypatch.setattr(session_finite, 'execute_finite_admission', gated)
    payloads = _wire(bot, monkeypatch)
    try:
        job = await asyncio.to_thread(_produce, bot)
        execution = executions.latest_execution(job['id'])['id']
        assert await asyncio.to_thread(scheduler.drain_delivery_queue, None, None) == 1
        await asyncio.wait_for(started.wait(), timeout=10)
        assert len(payloads) == 1
        assert len(_admissions(bot)) == 1
        assert (bot.home / 'producer-calls').read_text().splitlines() == ['executed']
        assert queue.get_status(execution)['status'] == 'pending'
        assert executions.latest_execution(job['id'])['delivery_outcome'] == 'queued'
        assert jobs.get_job(job['id'])['last_status'] == 'delivery_queued'
        assert await asyncio.to_thread(scheduler.drain_delivery_queue, None, None) == 0
        assert queue.get_status(execution)['status'] == 'pending'
        assert len(payloads) == 1
        assert len(_admissions(bot)) == 1

        release.set()
        await _settle_quietly(bot)
        assert await asyncio.to_thread(scheduler.drain_delivery_queue, None, None) == 1
        assert queue.get_status(execution)['status'] == 'delivered'
        assert executions.latest_execution(job['id'])['delivery_outcome'] == 'delivered'
        assert jobs.get_job(job['id'])['last_status'] == 'ok'
        assert len(payloads) == 1
        assert len(_admissions(bot)) == 1
    finally:
        release.set()
        await _settle_quietly(bot)


@pytest.mark.asyncio
async def test_waiting_bot_admission_does_not_wake_queue_watch_indefinitely(sealed_bot, monkeypatch):
    from gateway import session_finite
    from gateway.run_delivery_queue_watch import DeliveryQueueWatch

    bot = sealed_bot
    started = asyncio.Event()
    release = asyncio.Event()
    original = session_finite.execute_finite_admission

    async def gated(authority, ref, row):
        started.set()
        await release.wait()
        return await original(authority, ref, row)

    monkeypatch.setattr(session_finite, 'execute_finite_admission', gated)
    payloads = _wire(bot, monkeypatch)
    try:
        job = await asyncio.to_thread(_produce, bot)
        execution = executions.latest_execution(job['id'])['id']
        watch = DeliveryQueueWatch(lambda: [bot.home], lambda: None)
        assert await asyncio.to_thread(scheduler.drain_delivery_queue, None, None) == 1
        await asyncio.wait_for(started.wait(), timeout=10)
        assert queue.get_status(execution)['status'] == 'pending'
        assert watch.changed()
        await asyncio.to_thread(scheduler.drain_delivery_queue, None, None)
        assert not watch.changed(), 'rechecking an unchanged pending receipt wakes the watcher again'
        assert queue.get_status(execution)['status'] == 'pending'
        assert len(payloads) == len(_admissions(bot)) == 1
    finally:
        release.set()
        await _settle_quietly(bot)


@pytest.mark.asyncio
async def test_offline_bot_deferral_stays_quiet_then_delivers_once(sealed_bot, monkeypatch):
    from cron.bot_chat_legacy import drain_legacy_pending
    from gateway.run_delivery_queue_watch import DeliveryQueueWatch
    from tools import bot_live_delivery as mailbox

    bot = sealed_bot
    payloads = _wire(bot, monkeypatch)
    live = mailbox.find_canonical_live_owner
    monkeypatch.setattr(mailbox, 'find_canonical_live_owner',
                        lambda home: (_ for _ in ()).throw(ValueError('offline')))
    job = await asyncio.to_thread(_produce, bot)
    execution = executions.latest_execution(job['id'])['id']
    watch = DeliveryQueueWatch(lambda: [bot.home], lambda: None)
    assert await asyncio.to_thread(scheduler.drain_delivery_queue, None, None) == 1
    assert watch.changed()
    assert await asyncio.to_thread(scheduler.drain_delivery_queue, None, None) == 0
    assert not watch.changed()
    assert queue.get_status(execution)['status'] == 'pending'
    assert not payloads and not _admissions(bot)

    monkeypatch.setattr(mailbox, 'find_canonical_live_owner', live)
    await asyncio.to_thread(drain_legacy_pending)
    await _settle_quietly(bot)
    assert await asyncio.to_thread(scheduler.drain_delivery_queue, None, None) == 1
    assert queue.get_status(execution)['status'] == 'delivered'
    assert len(payloads) == len(_admissions(bot)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', ['malformed', 'unreadable'])
@pytest.mark.parametrize('failure', [False, True])
async def test_unreadable_deferred_record_waits_then_recovers(sealed_bot, monkeypatch, damage, failure):
    from pathlib import Path
    from cron.bot_chat_legacy import drain_legacy_pending
    from tools import bot_live_delivery as mailbox

    bot = sealed_bot
    payloads = _wire(bot, monkeypatch)
    live = mailbox.find_canonical_live_owner
    monkeypatch.setattr(mailbox, 'find_canonical_live_owner',
                        lambda home: (_ for _ in ()).throw(ValueError('offline')))
    job = await asyncio.to_thread(_produce, bot)
    execution = executions.latest_execution(job['id'])['id']
    if failure:
        with queue._transaction() as conn:
            conn.execute('UPDATE deliveries SET for_failure=1 WHERE execution_id=?', (execution,))
    assert await asyncio.to_thread(scheduler.drain_delivery_queue, None, None) == 1
    (pending,) = (bot.home / 'cron' / 'bot_chat_pending').glob('*.json')
    before = pending.read_text()
    if failure:
        (bot.home / 'config.yaml').write_text('display: {suppress_warning_notifications: true}\n')
    if damage == 'malformed':
        pending.write_text('{')
    else:
        original = Path.read_text

        def unreadable(path, *args, **kwargs):
            if path == pending:
                raise PermissionError('fixture unreadable')
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, 'read_text', unreadable)
    assert await asyncio.to_thread(scheduler.drain_delivery_queue, None, None) == 0
    assert queue.get_status(execution)['status'] == 'pending'
    assert not payloads and not _admissions(bot)
    if damage == 'malformed':
        pending.write_text(before)
    else:
        monkeypatch.setattr(Path, 'read_text', original)
    if failure:
        (bot.home / 'config.yaml').write_text('display: {suppress_warning_notifications: false}\n')
    monkeypatch.setattr(mailbox, 'find_canonical_live_owner', live)
    await asyncio.to_thread(drain_legacy_pending)
    await _settle_quietly(bot)
    assert await asyncio.to_thread(scheduler.drain_delivery_queue, None, None) == 1
    assert queue.get_status(execution)['status'] == 'delivered'
    assert await asyncio.to_thread(scheduler.drain_delivery_queue, None, None) == 0
    assert len(payloads) == len(_admissions(bot)) == 1


@pytest.mark.asyncio
async def test_unknown_queue_with_never_admitted_parked_consumer_delivers_once(sealed_bot, monkeypatch):
    from cron.bot_chat_legacy import drain_legacy_pending
    from cron.strict_delivery import UnknownDelivery
    from tools import bot_live_delivery as mailbox

    bot = sealed_bot
    payloads = _wire(bot, monkeypatch)
    live = mailbox.find_canonical_live_owner
    monkeypatch.setattr(mailbox, 'find_canonical_live_owner',
                        lambda home: (_ for _ in ()).throw(ValueError('offline')))
    job = await asyncio.to_thread(_produce, bot)
    execution = executions.latest_execution(job['id'])['id']
    assert await asyncio.to_thread(scheduler.drain_delivery_queue, None, None) == 1
    claimed = await asyncio.to_thread(queue.claim_next, execution_id=execution)
    assert claimed is not None
    assert await asyncio.to_thread(queue._finish, execution, error=UnknownDelivery('injected uncertainty'))
    assert queue.get_status(execution)['status'] == 'unknown'
    monkeypatch.setattr(mailbox, 'find_canonical_live_owner', live)
    await asyncio.to_thread(drain_legacy_pending)
    await _settle_quietly(bot)
    assert len(payloads) == len(_admissions(bot)) == 1
    await asyncio.to_thread(drain_legacy_pending)
    assert await asyncio.to_thread(scheduler.drain_delivery_queue, None, None) == 0
    assert len(payloads) == len(_admissions(bot)) == 1
    assert queue.get_status(execution)['status'] == 'unknown'


@pytest.mark.asyncio
@pytest.mark.parametrize('stamp', ['owner-gone', 'unstamped'])
async def test_dead_legacy_consumer_with_claimed_delivery_settles_without_replay(sealed_bot, monkeypatch, stamp):
    import json
    from tests.cron.test_completed_result_recovery import _python
    from tools import bot_live_delivery as mailbox

    bot = sealed_bot
    payloads = _wire(bot, monkeypatch)
    monkeypatch.setattr(mailbox, 'find_canonical_live_owner',
                        lambda home: (_ for _ in ()).throw(ValueError('offline')))
    job = await asyncio.to_thread(_produce, bot)
    execution = executions.latest_execution(job['id'])['id']
    assert await asyncio.to_thread(scheduler.drain_delivery_queue, None, None) == 1
    (pending,) = (bot.home / 'cron' / 'bot_chat_pending').glob('*.json')
    child = await asyncio.to_thread(_python, bot.home, '''
        import json, os, sys
        from cron.bot_chat_legacy import drain_legacy_pending
        from tools import bot_live_delivery as mailbox
        info = json.loads(sys.stdin.read())
        mailbox.find_canonical_live_owner = lambda home: info
        mailbox.deliver_to_live_owner = lambda *args, **kwargs: os._exit(88)
        drain_legacy_pending()
        ''', input=json.dumps({'profile_home': str(bot.home), 'session_id': bot.chat,
                               'live_session_id': bot.chat, 'lease_id': bot.authority.instance_id}))
    assert child.returncode == 88, (child.stdout, child.stderr)
    record = json.loads(pending.read_text())
    assert record['status'] == 'claimed'
    if stamp == 'unstamped':
        # Only a release that predates the owner stamp writes ``claimed`` without one.
        pending.write_text(json.dumps(
            {k: v for k, v in record.items() if k not in {'owner_pid', 'owner_started_at'}}))
    assert await asyncio.to_thread(scheduler.drain_delivery_queue, None, None) == 1
    assert queue.get_status(execution)['status'] == 'unknown'
    assert await asyncio.to_thread(scheduler.drain_delivery_queue, None, None) == 0
    assert not payloads and not _admissions(bot)


@pytest.mark.asyncio
async def test_live_legacy_consumer_keeps_claimed_delivery_pending(sealed_bot, monkeypatch):
    import json
    import os
    import sys
    import textwrap
    from tools import bot_live_delivery as mailbox

    bot = sealed_bot
    payloads = _wire(bot, monkeypatch)
    monkeypatch.setattr(mailbox, 'find_canonical_live_owner',
                        lambda home: (_ for _ in ()).throw(ValueError('offline')))
    job = await asyncio.to_thread(_produce, bot)
    execution = executions.latest_execution(job['id'])['id']
    assert await asyncio.to_thread(scheduler.drain_delivery_queue, None, None) == 1
    (pending,) = (bot.home / 'cron' / 'bot_chat_pending').glob('*.json')
    code = textwrap.dedent('''
        import json, sys
        from cron.bot_chat_legacy import drain_legacy_pending
        from tools import bot_live_delivery as mailbox
        owner = json.loads(sys.argv[1])
        mailbox.find_canonical_live_owner = lambda home: owner
        def hold(*args, **kwargs):
            print('claimed', flush=True)
            sys.stdin.read(1)
            raise OSError('unknown acknowledgement')
        mailbox.deliver_to_live_owner = hold
        drain_legacy_pending()
    ''')
    owner = {'profile_home': str(bot.home), 'session_id': bot.chat,
             'live_session_id': bot.chat, 'lease_id': bot.authority.instance_id}
    child = await asyncio.create_subprocess_exec(
        sys.executable, '-c', code, json.dumps(owner), env=dict(os.environ, HERMES_HOME=str(bot.home)),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        assert await asyncio.wait_for(child.stdout.readline(), timeout=15) == b'claimed\n'
        record = json.loads(pending.read_text())
        assert record['status'] == 'claimed' and record['owner_pid'] == child.pid
        assert await asyncio.to_thread(scheduler.drain_delivery_queue, None, None) == 0
        assert queue.get_status(execution)['status'] == 'pending'
    finally:
        if child.returncode is None:
            child.stdin.write(b'x')
            await child.stdin.drain()
            child.stdin.close()
        await asyncio.wait_for(child.wait(), timeout=15)
    assert child.returncode == 0
    assert json.loads(pending.read_text())['status'] == 'ambiguous'
    assert await asyncio.to_thread(scheduler.drain_delivery_queue, None, None) == 1
    assert queue.get_status(execution)['status'] == 'unknown'
    assert not payloads and not _admissions(bot)


@pytest.mark.asyncio
@pytest.mark.parametrize('accepted', [False, True])
async def test_offline_delivery_with_lost_ack_is_not_sent_again(sealed_bot, monkeypatch, accepted):
    import json
    from cron.bot_chat_legacy import drain_legacy_pending
    from tools import bot_live_delivery as mailbox

    bot = sealed_bot
    payloads = _wire(bot, monkeypatch)
    live = mailbox.find_canonical_live_owner
    door = mailbox.authority_delivery
    monkeypatch.setattr(mailbox, 'find_canonical_live_owner',
                        lambda home: (_ for _ in ()).throw(ValueError('offline')))
    job = await asyncio.to_thread(_produce, bot)
    execution = executions.latest_execution(job['id'])['id']
    assert await asyncio.to_thread(scheduler.drain_delivery_queue, None, None) == 1
    (pending,) = (bot.home / 'cron' / 'bot_chat_pending').glob('*.json')
    monkeypatch.setattr(mailbox, 'find_canonical_live_owner', live)
    calls = []
    def lose_ack(*args, **kwargs):
        calls.append(1)
        if accepted:
            door(*args, **kwargs)
        raise OSError('ACK lost')
    monkeypatch.setattr(mailbox, 'authority_delivery', lose_ack)
    await asyncio.to_thread(drain_legacy_pending)
    assert json.loads(pending.read_text())['status'] == 'ambiguous'
    if accepted:
        await _settle_quietly(bot)
    monkeypatch.setattr(mailbox, 'authority_delivery', door)
    await asyncio.to_thread(scheduler.drain_delivery_queue, None, None)
    await _settle_quietly(bot)
    state = queue.get_status(execution)
    assert len(calls) == 1
    assert (state['status'], len(payloads), len(_admissions(bot))) == (
        ('delivered', 1, 1) if accepted else ('unknown', 0, 0)
    ), (state, payloads)


@pytest.mark.asyncio
async def test_receipt_appearing_during_deferred_handoff_is_rechecked(sealed_bot, monkeypatch):
    import json
    from cron.bot_chat_legacy import drain_legacy_pending
    from cron.scheduler_delivery import _deliver_to_bot_chat
    from tools import bot_live_delivery as mailbox

    bot = sealed_bot
    payloads = _wire(bot, monkeypatch)
    live = mailbox.find_canonical_live_owner
    monkeypatch.setattr(mailbox, 'find_canonical_live_owner',
                        lambda home: (_ for _ in ()).throw(ValueError('offline')))
    job = await asyncio.to_thread(_produce, bot)
    execution = executions.latest_execution(job['id'])['id']
    assert await asyncio.to_thread(scheduler.drain_delivery_queue, None, None) == 1
    monkeypatch.setattr(mailbox, 'find_canonical_live_owner', live)
    await asyncio.to_thread(drain_legacy_pending)
    await _settle_quietly(bot)
    row = queue.get_status(execution)
    saved_job = json.loads(row['job_json'])
    read = mailbox.read_delivery_result
    reads = []

    def concurrent_read(*args):
        reads.append(1)
        return None if len(reads) == 1 else read(*args)

    monkeypatch.setattr(mailbox, 'read_delivery_result', concurrent_read)
    result = await asyncio.to_thread(_deliver_to_bot_chat, saved_job, row['content'], '')
    assert result is None
    assert len(reads) == 2 and len(payloads) == len(_admissions(bot)) == 1


@pytest.mark.asyncio
async def test_parked_failure_waits_for_durable_suppression(sealed_bot, monkeypatch):
    import json
    from cron.bot_chat_legacy import drain_legacy_pending
    from tools import bot_live_delivery as mailbox

    bot = sealed_bot
    payloads = _wire(bot, monkeypatch)
    monkeypatch.setattr(mailbox, 'find_canonical_live_owner',
                        lambda home: (_ for _ in ()).throw(ValueError('offline')))
    job = await asyncio.to_thread(_produce, bot)
    execution = executions.latest_execution(job['id'])['id']
    with queue._transaction() as conn:
        conn.execute('UPDATE deliveries SET for_failure=1 WHERE execution_id=?', (execution,))
    assert await asyncio.to_thread(scheduler.drain_delivery_queue, None, None) == 1
    (pending,) = (bot.home / 'cron' / 'bot_chat_pending').glob('*.json')
    assert json.loads(pending.read_text())['status'] == 'queued'
    (bot.home / 'config.yaml').write_text('display: {suppress_warning_notifications: true}\n')
    assert await asyncio.to_thread(scheduler.drain_delivery_queue, None, None) == 0
    assert queue.get_status(execution)['status'] == 'pending'
    await asyncio.to_thread(drain_legacy_pending)
    assert json.loads(pending.read_text())['status'] == 'suppressed'
    (bot.home / 'config.yaml').write_text('display: {suppress_warning_notifications: false}\n')
    assert await asyncio.to_thread(scheduler.drain_delivery_queue, None, None) == 1
    assert queue.get_status(execution)['status'] == 'suppressed'
    assert not payloads and not _admissions(bot)


@pytest.mark.asyncio
async def test_settled_bot_failure_stays_unknown_and_is_not_retried(sealed_bot, monkeypatch):
    from tools import bot_live_delivery as mailbox

    bot = sealed_bot
    payloads = _wire(bot, monkeypatch)
    bot.errors[:] = ['HTTP 503 server error']
    job = await asyncio.to_thread(_produce, bot)
    execution = executions.latest_execution(job['id'])['id']

    assert await asyncio.to_thread(scheduler.drain_delivery_queue, None, None) == 1
    await _settle_quietly(bot)
    assert await asyncio.to_thread(scheduler.drain_delivery_queue, None, None) == 1
    assert queue.get_status(execution)['status'] == 'unknown'
    assert len(payloads) == 1
    assert len(_admissions(bot)) == 1
    assert await asyncio.to_thread(scheduler.drain_delivery_queue, None, None) == 0
    assert mailbox.read_delivery_result(bot.home, payloads[0]['id'])['status'] == 'failed'