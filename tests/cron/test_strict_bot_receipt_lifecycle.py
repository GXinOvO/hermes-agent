import asyncio
import json

import pytest

from cron import delivery_queue as queue, scheduler
from gateway.config import Platform
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
@pytest.mark.parametrize('case', ['settled', 'queued', 'conflicting'])
async def test_receipt_appearing_after_preflight_is_not_resent(sealed_bot, monkeypatch, case):
    from gateway import session_finite
    from tests.gateway.test_session_bot_retry import _admissions, _settle_quietly

    release = asyncio.Event()
    original_execute = session_finite.execute_finite_admission

    async def gated(authority, ref, row):
        await release.wait()
        return await original_execute(authority, ref, row)

    if case == 'queued':
        monkeypatch.setattr(session_finite, 'execute_finite_admission', gated)
    async with cached_fanout(sealed_bot, monkeypatch) as test:
        loop = asyncio.get_running_loop()
        claim = queue.claim_next
        injected = []

        async def admit_after_preflight():
            response = await test.connection.dispatch({'id': 73, 'method': 'bot_relay.deliver',
                                                       'params': test.identity['params']})
            assert 'result' in response, response
            if case != 'queued':
                await _settle_quietly(sealed_bot)
            if case == 'conflicting':
                receipt = json.loads(test.path.read_text())
                receipt['message'] = 'different payload'
                test.path.write_text(json.dumps(receipt))
            injected.append(response['result']['admission_id'])

        def claim_and_admit(**kwargs):
            row = claim(**kwargs)
            if row is not None and not injected:
                asyncio.run_coroutine_threadsafe(admit_after_preflight(), loop).result(timeout=20)
            return row

        monkeypatch.setattr(queue, 'claim_next', claim_and_admit)
        try:
            assert await asyncio.to_thread(scheduler.drain_delivery_queue, {Platform.SLACK: test.adapter}, loop) == 1
            assert len(injected) == len(_admissions(sealed_bot)) == 1
            assert not any(rpc['method'] == 'bot_relay.deliver' for rpc in test.rpcs)
            row = queue.get_status(test.row['execution_id'])
            assert row is not None
            assert row['status'] == {'settled': 'delivered', 'queued': 'pending', 'conflicting': 'unknown'}[case]
        finally:
            release.set()
            await _settle_quietly(sealed_bot)
        if case == 'queued':
            assert await asyncio.to_thread(scheduler.drain_delivery_queue, {Platform.SLACK: test.adapter}, loop) == 1
            final = queue.get_status(test.row['execution_id'])
            assert final is not None and final['status'] == 'delivered'
            assert not any(rpc['method'] == 'bot_relay.deliver' for rpc in test.rpcs)


@pytest.mark.asyncio
async def test_queued_bot_target_resumes_mixed_fanout_without_replaying_done_prefix(sealed_bot, monkeypatch):
    from gateway import session_finite
    from gateway.run_delivery_queue_watch import DeliveryQueueWatch
    from tests.gateway.test_session_bot_retry import _admissions, _settle_quietly

    started = asyncio.Event()
    release = asyncio.Event()
    original = session_finite.execute_finite_admission

    async def gated(authority, ref, row):
        started.set()
        await release.wait()
        return await original(authority, ref, row)

    monkeypatch.setattr(session_finite, 'execute_finite_admission', gated)
    async with cached_fanout(sealed_bot, monkeypatch) as test:
        loop = asyncio.get_running_loop()
        processed = await asyncio.to_thread(scheduler.drain_delivery_queue, {Platform.SLACK: test.adapter}, loop)
        await asyncio.wait_for(started.wait(), timeout=10)
        with queue._transaction() as conn:
            pending = dict(conn.execute('SELECT * FROM deliveries WHERE execution_id=?',
                                        (test.row['execution_id'],)).fetchone())
        first_event_count = len(test.events)
        assert processed == 1 and pending['status'] == 'pending'
        assert len(_admissions(sealed_bot)) == 1
        watch = DeliveryQueueWatch(lambda: [sealed_bot.home], lambda: None)
        try:
            stamps = []
            for _ in range(5):
                assert await asyncio.to_thread(scheduler.drain_delivery_queue, {Platform.SLACK: test.adapter}, loop) == 0
                stamps.append((watch.changed(), watch._seen))
            assert not any(changed for changed, _ in stamps[-2:]), stamps
            assert len(test.events) == first_event_count and len(_admissions(sealed_bot)) == 1
        finally:
            release.set()
        await _settle_quietly(sealed_bot)
        processed = await asyncio.to_thread(scheduler.drain_delivery_queue, {Platform.SLACK: test.adapter}, loop)
        await _settle_quietly(sealed_bot)
        with queue._transaction() as conn:
            delivered = dict(conn.execute('SELECT * FROM deliveries WHERE execution_id=?',
                                          (test.row['execution_id'],)).fetchone())
        assert processed == 1 and delivered['status'] == 'delivered'
        assert len(_admissions(sealed_bot)) == 1
        assert len(test.events) == first_event_count + 1
        assert test.events[0]['data']['channel'] == 'C12345678'
        assert test.events[-1]['data']['channel'] == 'CLOCAL'


@pytest.mark.asyncio
async def test_ambiguous_bot_receipt_finishes_fanout_unknown_without_replay(sealed_bot, monkeypatch):
    from gateway import session_bot, session_finite
    from tests.gateway.test_session_bot_retry import _admissions, _settle_quietly

    started, release = asyncio.Event(), asyncio.Event()
    original = session_finite.execute_finite_admission

    async def gated(authority, ref, row):
        started.set()
        await release.wait()
        return await original(authority, ref, row)

    monkeypatch.setattr(session_finite, 'execute_finite_admission', gated)
    async with cached_fanout(sealed_bot, monkeypatch) as test:
        loop = asyncio.get_running_loop()
        admission_id = None
        try:
            assert await asyncio.to_thread(scheduler.drain_delivery_queue, {Platform.SLACK: test.adapter}, loop) == 1
            await asyncio.wait_for(started.wait(), timeout=10)
            admission_id = _admissions(sealed_bot)[0]['admission_id']
            sealed_bot.authority.db._execute_write(lambda conn: conn.execute(
                "UPDATE session_admissions SET status='unknown' WHERE admission_id=?", (admission_id,)))
            _, receipt = await session_bot.read_receipt(sealed_bot.home, test.identity['params']['id'])
            result = await session_bot._publish_result(sealed_bot.authority, sealed_bot.home, receipt)
            assert result['status'] == 'ambiguous'
            assert json.loads(test.path.read_text())['status'] == 'ambiguous'
            previous_events = list(test.events)
            previous_rpcs = list(test.rpcs)
            assert await asyncio.to_thread(scheduler.drain_delivery_queue, {Platform.SLACK: test.adapter}, loop) == 1
            assert queue.get_status(test.row['execution_id'])['status'] == 'unknown'
            assert await asyncio.to_thread(scheduler.drain_delivery_queue, {Platform.SLACK: test.adapter}, loop) == 0
            assert test.events == previous_events and test.rpcs == previous_rpcs
            assert len(_admissions(sealed_bot)) == 1
        finally:
            if admission_id is not None:
                sealed_bot.authority.db._execute_write(lambda conn: conn.execute(
                    "UPDATE session_admissions SET status='started' WHERE admission_id=?", (admission_id,)))
            release.set()
            await _settle_quietly(sealed_bot)


@pytest.mark.asyncio
async def test_refused_bot_receipt_finishes_fanout_unknown_without_replay(sealed_bot, monkeypatch):
    from hermes_state_runtime import RuntimeStoreError
    from tests.gateway.test_session_bot_retry import _admissions

    async with cached_fanout(sealed_bot, monkeypatch) as test:
        def refuse(*args, **kwargs):
            raise RuntimeStoreError('permission_denied')

        with monkeypatch.context() as patch:
            patch.setattr('gateway.session_automation.snapshot_automation', refuse)
            response = await test.connection.dispatch(
                {'id': 10, 'method': 'bot_relay.deliver', 'params': test.identity['params']})
        assert response.get('error', {}).get('data', {}).get('reason') == 'permission_denied', response
        receipt = json.loads(test.path.read_text())
        assert receipt['status'] == 'failed' and receipt['refused'] is True
        assert not _admissions(sealed_bot)
        rpcs_before = list(test.rpcs)
        loop = asyncio.get_running_loop()
        assert await asyncio.to_thread(scheduler.drain_delivery_queue, {Platform.SLACK: test.adapter}, loop) == 1
        assert queue.get_status(test.row['execution_id'])['status'] == 'unknown'
        assert await asyncio.to_thread(scheduler.drain_delivery_queue, {Platform.SLACK: test.adapter}, loop) == 0
        assert not test.events and test.rpcs == rpcs_before and not _admissions(sealed_bot)


@pytest.mark.asyncio
@pytest.mark.parametrize('phase,field', [('preflight', 'message'), ('preflight', 'principal_id'),
                                       ('preflight', 'seal'), ('after_claim', 'seal')])
async def test_refused_receipt_identity_conflicts_never_send(sealed_bot, monkeypatch, phase, field):
    from hermes_state_runtime import RuntimeStoreError
    from tests.gateway.test_session_bot_retry import _admissions

    async with cached_fanout(sealed_bot, monkeypatch) as test:
        def refuse(*args, **kwargs):
            raise RuntimeStoreError('permission_denied')

        with monkeypatch.context() as patch:
            patch.setattr('gateway.session_automation.snapshot_automation', refuse)
            response = await test.connection.dispatch(
                {'id': 10, 'method': 'bot_relay.deliver', 'params': test.identity['params']})
        assert response.get('error', {}).get('data', {}).get('reason') == 'permission_denied', response

        def corrupt():
            receipt = json.loads(test.path.read_text())
            if field == 'seal':
                receipt['sealed_cron_result_v1']['seal'] = '0' * 64
            else:
                receipt[field] = 'foreign'
            test.path.write_text(json.dumps(receipt))

        if phase == 'preflight':
            corrupt()
        else:
            claim = queue.claim_next

            def corrupt_after_claim(**kwargs):
                row = claim(**kwargs)
                if row is not None:
                    corrupt()
                return row

            monkeypatch.setattr(queue, 'claim_next', corrupt_after_claim)
        loop = asyncio.get_running_loop()
        processed = await asyncio.to_thread(scheduler.drain_delivery_queue, {Platform.SLACK: test.adapter}, loop)
        row = queue.get_status(test.row['execution_id'])
        assert processed == (0 if phase == 'preflight' else 1)
        assert row['status'] == ('pending' if phase == 'preflight' else 'unknown')
        assert not test.events and not _admissions(sealed_bot)
        assert not any(rpc['method'] == 'bot_relay.deliver' for rpc in test.rpcs)


@pytest.mark.asyncio
async def test_waiting_fanout_does_not_block_later_ready_delivery(sealed_bot, monkeypatch):
    from cron import jobs
    from gateway import session_finite
    from tests.gateway.test_session_bot_retry import _admissions, _settle_quietly

    started, release = asyncio.Event(), asyncio.Event()
    original = session_finite.execute_finite_admission

    async def gated(authority, ref, row):
        started.set()
        await release.wait()
        return await original(authority, ref, row)

    monkeypatch.setattr(session_finite, 'execute_finite_admission', gated)
    async with cached_fanout(sealed_bot, monkeypatch) as test:
        loop = asyncio.get_running_loop()
        try:
            assert await asyncio.to_thread(scheduler.drain_delivery_queue, {Platform.SLACK: test.adapter}, loop) == 1
            await asyncio.wait_for(started.wait(), timeout=10)
            prefix_events = list(test.events)
            job = jobs.create_job(prompt=None, schedule='every 1h', script='cache-producer.py', no_agent=True,
                                  deliver='slack:CLOCAL')
            assert await asyncio.to_thread(scheduler._run_one_job_body, job)
            with queue._transaction() as conn:
                ready = conn.execute('SELECT execution_id FROM deliveries WHERE execution_id<>?',
                                     (test.row['execution_id'],)).fetchone()[0]
            assert await asyncio.to_thread(scheduler.drain_delivery_queue, {Platform.SLACK: test.adapter}, loop) == 1
            assert queue.get_status(test.row['execution_id'])['status'] == 'pending'
            assert queue.get_status(ready)['status'] == 'delivered'
            assert len(test.events) == len(prefix_events) + 1
            assert test.events[:len(prefix_events)] == prefix_events
            assert test.events[-1]['data']['channel'] == 'CLOCAL'
            assert len(_admissions(sealed_bot)) == 1
        finally:
            release.set()
            await _settle_quietly(sealed_bot)
        assert await asyncio.to_thread(scheduler.drain_delivery_queue, {Platform.SLACK: test.adapter}, loop) == 1
        assert queue.get_status(test.row['execution_id'])['status'] == 'delivered'
        assert len(test.events) == len(prefix_events) + 2
        assert len(_admissions(sealed_bot)) == 1


@pytest.mark.asyncio
async def test_settled_bot_receipt_survives_authority_restart_without_re_admission(sealed_bot, monkeypatch):
    from gateway import session_finite
    from hermes_cli import gateway_runtime
    from tests.gateway.test_session_bot_retry import _admissions, _settle_quietly

    started = asyncio.Event()
    release = asyncio.Event()
    original = session_finite.execute_finite_admission

    async def gated(authority, ref, row):
        started.set()
        await release.wait()
        return await original(authority, ref, row)

    monkeypatch.setattr(session_finite, 'execute_finite_admission', gated)
    async with cached_fanout(sealed_bot, monkeypatch) as test:
        loop = asyncio.get_running_loop()
        assert await asyncio.to_thread(scheduler.drain_delivery_queue, {Platform.SLACK: test.adapter}, loop) == 1
        await asyncio.wait_for(started.wait(), timeout=10)
        release.set()
        await _settle_quietly(sealed_bot)

        changed = gateway_runtime.GatewayEndpoint(
            str(sealed_bot.home), 'restarted-owner', 2, 1, 'http://127.0.0.1:9', 'none',
            frozenset({'session-authority-v1'}))
        monkeypatch.setattr(
            gateway_runtime, 'discover_gateway_endpoint',
            lambda *args, **kwargs: gateway_runtime.GatewayDiscovery('ready', changed),
        )
        assert await asyncio.to_thread(scheduler.drain_delivery_queue, {Platform.SLACK: test.adapter}, loop) == 1
        await _settle_quietly(sealed_bot)
        with queue._transaction() as conn:
            row = dict(conn.execute('SELECT * FROM deliveries WHERE execution_id=?',
                                    (test.row['execution_id'],)).fetchone())
        assert row['status'] == 'delivered'
        assert len(_admissions(sealed_bot)) == 1


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
