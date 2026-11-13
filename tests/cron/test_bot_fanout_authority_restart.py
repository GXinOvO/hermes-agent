import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from cron import delivery_queue as queue, scheduler
from gateway.config import Platform
from tests.cron.test_strict_bot_receipt_cases import cached_fanout
from tests.gateway.test_session_bot_retry import _admissions, _settle_quietly

pytest_plugins = ['tests.cron.test_sealed_bot_consumer']


@pytest.mark.asyncio
@pytest.mark.parametrize('scenario', ['resume', 'target_changed', 'missing_receipt', 'legacy_missing_receipt',
                                      'waiting_owner_crash', 'changed_admission', 'legacy_old_owner_missing_receipt',
                                      'legacy_missing_ack', 'legacy_false_ack'])
async def test_authority_restart_before_bot_admission_preserves_completed_prefix(sealed_bot, monkeypatch, scenario):
    from cron import strict_thread_delivery as stages
    from gateway import session_finite
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway.session_authority import initialize_session_authority
    from hermes_state import SessionDB
    from hermes_cli import gateway_runtime

    async with cached_fanout(sealed_bot, monkeypatch) as case:
        loop = asyncio.get_running_loop()
        original_transition = stages.transition
        stopped = []

        def stop_after_prefix(execution, index, before, after, receipt=None):
            result = original_transition(execution, index, before, after, receipt)
            if result and index == 0 and after == 'done':
                stopped.append(True)
                raise SystemExit('simulated owner exit after durable prefix')
            return result

        with monkeypatch.context() as fault:
            fault.setattr(stages, 'transition', stop_after_prefix)
            fault.setattr(queue, '_finish', lambda *args, **kwargs: False)
            assert await asyncio.to_thread(scheduler.drain_delivery_queue, {Platform.SLACK: case.adapter}, loop) == 1

        row = queue.get_status(case.row['execution_id'])
        assert row is not None
        bundle = json.loads(row['thread_json'])
        assert stopped == [True] and row['status'] == 'delivering'
        assert [item['stage'] for item in bundle['items']] == ['done', 'initial', 'initial']
        assert len(case.events) == 1 and not case.path.exists() and not _admissions(sealed_bot)
        prefix_receipt = bundle['items'][0]['receipt']
        old_endpoint = gateway_runtime.discover_gateway_endpoint(sealed_bot.home).endpoint
        assert old_endpoint is not None
        old_authority = sealed_bot.authority
        store = SessionStore(sealed_bot.home / 'sessions', GatewayConfig())
        store._db = SessionDB(db_path=sealed_bot.home / 'state.db')
        runner = SimpleNamespace(session_store=store, _session_db=store._db, adapters={}, _draining=False,
                                 _evict_cached_agent=lambda route: None)
        runner._adapter_for_source = lambda source: runner.adapters.get(source.platform)
        replacement = await initialize_session_authority(runner,
            profile_id=old_authority.profile_id, instance_id='replacement-owner')
        old_authority.retired = True
        sealed_bot.authority = replacement
        case.connection.authority = replacement
        replacement.events[case.connection.actor.transport_id] = case.connection.transport
        endpoint = replace(old_endpoint, instance_id=replacement.instance_id, authority_epoch=replacement.epoch)
        monkeypatch.setattr(gateway_runtime, 'discover_gateway_endpoint',
                            lambda *args, **kwargs: gateway_runtime.GatewayDiscovery('ready', endpoint))
        with queue._transaction() as conn:
            conn.execute("UPDATE deliveries SET owner_process_id='dead-fixture', owner_pid=2000000000, "
                         "owner_started_at=1 WHERE execution_id=?", (case.row['execution_id'],))
        if scenario == 'target_changed':
            assert replacement.db.set_session_title(sealed_bot.chat, 'Previous Chat')
            assert await asyncio.to_thread(scheduler.drain_delivery_queue, {Platform.SLACK: case.adapter}, loop) == 0
            changed_status = queue.get_status(case.row['execution_id'])
            assert changed_status is not None and changed_status['status'] == 'pending'
            assert len(case.events) == 1 and not case.path.exists() and not _admissions(sealed_bot)
            assert not any(r['method'] == 'bot_relay.deliver' for r in case.rpcs)
        else:
            started, release = asyncio.Event(), asyncio.Event()
            original_execute = session_finite.execute_finite_admission

            async def held(authority, ref, admission):
                started.set()
                await release.wait()
                return await original_execute(authority, ref, admission)

            monkeypatch.setattr(session_finite, 'execute_finite_admission', held)
            receipt_before = None
            try:
                with monkeypatch.context() as fault:
                    if scenario == 'waiting_owner_crash':
                        fault.setattr(queue, '_finish', lambda *args, **kwargs: False)
                    assert await asyncio.to_thread(scheduler.drain_delivery_queue, {Platform.SLACK: case.adapter}, loop) == 1
                await asyncio.wait_for(started.wait(), timeout=10)
                parked = queue.get_status(case.row['execution_id'])
                assert parked is not None
                assert parked['status'] == ('delivering' if scenario == 'waiting_owner_crash' else 'pending')
                rebound = json.loads(parked['thread_json'])
                assert rebound['items'][0]['stage'] == 'done'
                assert rebound['items'][0]['receipt'] == prefix_receipt
                assert rebound['items'][1]['stage'] == 'bot_waiting'
                assert rebound['items'][1]['receipt']['admission_id'] == _admissions(sealed_bot)[0]['admission_id']
                identity = rebound['binding']['targets'][1]['plan']['identity']
                assert identity['endpoint']['instance_id'] == replacement.instance_id
                assert identity['params']['sealed_cron_result_v1']['owner_instance_id'] == replacement.instance_id
                if scenario == 'waiting_owner_crash':
                    with queue._transaction() as conn:
                        conn.execute("UPDATE deliveries SET owner_process_id='dead-fixture', owner_pid=2000000000, "
                                     "owner_started_at=1 WHERE execution_id=?", (case.row['execution_id'],))
                if scenario == 'changed_admission':
                    receipt_before = case.path.read_text()
                    changed = json.loads(receipt_before)
                    changed['admission_id'] = 'f' * 32
                    case.path.write_text(json.dumps(changed))
                assert await asyncio.to_thread(scheduler.drain_delivery_queue, {Platform.SLACK: case.adapter}, loop) == 0
                assert queue.get_status(case.row['execution_id'])['status'] == 'pending'
                assert len(case.events) == 1 and len(_admissions(sealed_bot)) == 1
                if scenario == 'changed_admission':
                    assert receipt_before is not None
                    case.path.write_text(receipt_before)
                if scenario in {'legacy_missing_receipt', 'legacy_old_owner_missing_receipt',
                                'legacy_missing_ack', 'legacy_false_ack'}:
                    rebound['items'][1] = {'stage': 'initial', 'receipt': None, 'receipt_sha256': None}
                    stages.validate(rebound)
                    with queue._transaction() as conn:
                        conn.execute('UPDATE deliveries SET thread_json=? WHERE execution_id=?',
                                     (stages.encode(rebound), case.row['execution_id']))
                if scenario in {'missing_receipt', 'legacy_missing_receipt', 'legacy_old_owner_missing_receipt',
                                'legacy_missing_ack', 'legacy_false_ack'}:
                    case.path.unlink()
                    with monkeypatch.context() as old_owner:
                        if scenario == 'legacy_old_owner_missing_receipt':
                            from tui_gateway.contracts import registry
                            from tui_gateway.contracts.canonical_projections import CanonicalBotDeliverParams

                            class LegacyPrepareParams(CanonicalBotDeliverParams):
                                create_missing: bool = False

                            contract = registry.CANONICAL_METHODS['bot_relay.prepare']
                            old_owner.setitem(registry.CANONICAL_METHODS, 'bot_relay.prepare',
                                              replace(contract, params=LegacyPrepareParams))
                            old_owner.setattr('gateway.session_bot._pin_committed', lambda *args: False)
                        elif scenario in {'legacy_missing_ack', 'legacy_false_ack'}:
                            dispatch = case.connection.dispatch
                            old_owner.setattr('gateway.session_bot._pin_committed', lambda *args: False)

                            async def old_answer(request):
                                answer = await dispatch(request)
                                if request.get('method') == 'bot_relay.prepare' and 'result' in answer:
                                    if scenario == 'legacy_missing_ack':
                                        answer['result'].pop('unadmitted_v1', None)
                                    else:
                                        answer['result']['unadmitted_v1'] = False
                                return answer

                            old_owner.setattr(case.connection, 'dispatch', old_answer)
                        await asyncio.to_thread(scheduler.drain_delivery_queue, {Platform.SLACK: case.adapter}, loop)
                    missing = queue.get_status(case.row['execution_id'])
                    assert missing is not None
                    assert missing['status'] == ('unknown' if scenario == 'missing_receipt' else 'pending')
                    assert await asyncio.to_thread(scheduler.drain_delivery_queue, {Platform.SLACK: case.adapter}, loop) == 0
                    assert len([r for r in case.rpcs if r['method'] == 'bot_relay.deliver']) == 1
                    assert len(_admissions(sealed_bot)) == 1 and len(case.events) == 1
            finally:
                release.set()
                await _settle_quietly(sealed_bot)
            if scenario in {'resume', 'waiting_owner_crash', 'changed_admission'}:
                assert await asyncio.to_thread(scheduler.drain_delivery_queue, {Platform.SLACK: case.adapter}, loop) == 1
                final = queue.get_status(case.row['execution_id'])
                assert final is not None
                assert final['status'] == 'delivered'
                assert [event['data']['channel'] for event in case.events] == ['C12345678', 'CLOCAL']
                assert len(_admissions(sealed_bot)) == 1
                assert len([r for r in case.rpcs if r['method'] == 'bot_relay.deliver']) == 1
                assert (sealed_bot.home / 'cache-producer-count').read_text().splitlines() == ['executed']
                assert await asyncio.to_thread(scheduler.drain_delivery_queue, {Platform.SLACK: case.adapter}, loop) == 0
