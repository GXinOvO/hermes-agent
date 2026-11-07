import asyncio
import json

import pytest

from cron import delivery_queue as queue, scheduler
from tests.cron.test_strict_bot_receipt_cases import cached_fanout, admit_cached, drain_cached, mutate_native

pytest_plugins = ['tests.cron.test_sealed_bot_consumer']


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['db_provenance', 'db_policy'])
async def test_native_provenance_required_before_any_claim(sealed_bot, monkeypatch, case):
    async with cached_fanout(sealed_bot, monkeypatch) as test:
        receipt = await admit_cached(test)
        mutate_native(sealed_bot, receipt, case)
        before = test.path.read_bytes()
        processed, final = await drain_cached(test)
        assert processed == 0 and final['status'] == 'pending'
        assert not test.events and test.path.read_bytes() == before
        assert not any(rpc['method'] == 'bot_relay.deliver' for rpc in test.rpcs)


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['reverse_category', 'suppressed_existing', 'legacy_live'])
async def test_cached_notification_and_legacy_controls(sealed_bot, monkeypatch, case):
    async with cached_fanout(sealed_bot, monkeypatch, diagnostic=case != 'legacy_live') as test:
        receipt = await admit_cached(test)
        if case == 'reverse_category':
            receipt.pop('notification_category')
        elif case == 'legacy_live':
            receipt.pop('native_admission_v1')
        else:
            (sealed_bot.home / 'config.yaml').write_text('display:\n  suppress_warning_notifications: true\n')
        test.path.write_text(json.dumps(receipt))
        before = test.path.read_bytes()
        processed, final = await drain_cached(test)
        assert processed == int(case != 'reverse_category')
        assert final['status'] == ('pending' if case == 'reverse_category' else 'delivered')
        assert len(test.events) == (0 if case == 'reverse_category' else 2)
        assert test.path.read_bytes() == before
        assert not any(rpc['method'] == 'bot_relay.deliver' for rpc in test.rpcs)


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['normal', 'missing_template', 'retired_digest', 'retired_principal'])
async def test_cached_ack_uses_real_retirement_evidence(sealed_bot, monkeypatch, case):
    from cron.strict_bot_delivery import freeze, send
    from hermes_state_terminal import ADMISSION_PREFIX, terminal_admission
    async with cached_fanout(sealed_bot, monkeypatch) as test:
        receipt = await admit_cached(test)
        if case == 'missing_template':
            receipt.pop('native_admission_v1')
            test.path.write_text(json.dumps(receipt))
        frozen, _ = await asyncio.to_thread(freeze, test.target, json.loads(test.row['job_json']), test.row['content'], False)
        assert sealed_bot.authority.db.delete_session(sealed_bot.chat, sessions_dir=sealed_bot.home / 'sessions')
        with sealed_bot.authority.db._read_ctx() as conn:
            retired = terminal_admission(conn, receipt['admission_id'])
            assert retired['payload_json'] == '{}' and retired['status'] == 'terminal'
            assert conn.execute('SELECT 1 FROM session_admissions WHERE admission_id=?', (receipt['admission_id'],)).fetchone() is None
        if case.startswith('retired_'):
            retired['payload_digest' if case == 'retired_digest' else 'principal_id'] = 'foreign'
            sealed_bot.authority.db._execute_write(lambda conn: conn.execute('UPDATE state_meta SET value=? WHERE key=?',
                (json.dumps(retired), ADMISSION_PREFIX + receipt['admission_id'])))
        before = test.path.read_bytes()
        if case == 'normal':
            ack = await asyncio.to_thread(send, frozen)
            assert ack['ok'] and ack['admission_id'] == receipt['admission_id']
        else:
            with pytest.raises(ValueError, match='strict Bot'):
                await asyncio.to_thread(send, frozen)
        assert test.path.read_bytes() == before and not test.events
        assert not any(rpc['method'] == 'bot_relay.deliver' for rpc in test.rpcs)
        assert (sealed_bot.home / 'cache-producer-count').read_text().splitlines() == ['executed']


@pytest.mark.asyncio
async def test_cached_ack_rechecked_after_claim(sealed_bot, monkeypatch):
    async with cached_fanout(sealed_bot, monkeypatch) as test:
        receipt = await admit_cached(test)
        claim = queue.claim_next
        touched = []

        def changed(**kwargs):
            row = claim(**kwargs)
            if row is not None:
                mutate_native(sealed_bot, receipt, 'db_provenance')
                touched.append(row['execution_id'])
            return row

        monkeypatch.setattr(queue, 'claim_next', changed)
        processed, final = await drain_cached(test)
        assert touched == [test.row['execution_id']]
        assert processed == 1 and final['status'] == 'unknown'
        assert len(test.events) == 1 and test.events[0]['data']['channel'] == 'C12345678'
        assert not any(rpc['method'] == 'bot_relay.deliver' for rpc in test.rpcs)


@pytest.mark.asyncio
async def test_damaged_cached_receipt_does_not_block_valid_queue_sibling(sealed_bot, monkeypatch):
    from cron import jobs
    from gateway.config import Platform
    from tests.gateway.test_session_bot_retry import _settle_quietly
    async with cached_fanout(sealed_bot, monkeypatch) as test:
        receipt = await admit_cached(test)
        receipt['admission_id'] = 'f' * 32
        test.path.write_text(json.dumps(receipt))
        before = test.path.read_bytes()
        job = jobs.create_job(prompt=None, schedule='every 1h', script='cache-producer.py', no_agent=True,
                              deliver='slack:CLOCAL')
        assert await asyncio.to_thread(scheduler._run_one_job_body, job)
        with queue._transaction() as conn:
            good = conn.execute('SELECT execution_id FROM deliveries WHERE execution_id<>?',
                                (test.row['execution_id'],)).fetchone()[0]
        loop = asyncio.get_running_loop()
        processed = await asyncio.to_thread(scheduler.drain_delivery_queue, {Platform.SLACK: test.adapter}, loop)
        await _settle_quietly(sealed_bot)
        again = await asyncio.to_thread(scheduler.drain_delivery_queue, {Platform.SLACK: test.adapter}, loop)
        with queue._transaction() as conn:
            rows = {row['execution_id']: dict(row) for row in conn.execute('SELECT * FROM deliveries')}
        assert processed == 1 and again == 0
        assert rows[test.row['execution_id']]['status'] == 'pending' and rows[good]['status'] == 'delivered'
        assert len(test.events) == 1 and test.events[0]['data']['channel'] == 'CLOCAL'
        assert test.path.read_bytes() == before
        assert (sealed_bot.home / 'cache-producer-count').read_text().splitlines() == ['executed', 'executed']
