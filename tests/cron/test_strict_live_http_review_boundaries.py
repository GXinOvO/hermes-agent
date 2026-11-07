import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from cron import delivery_queue as queue, scheduler, scheduler_delivery as dispatch
from cron import strict_thread_delivery as stages
from cron.scheduler_preflight import SharedRouteAdapters
from gateway.config import GatewayConfig, Platform
from plugins.platforms.weixin import frozen_delivery as helper
from tests.gateway import frozen_live_http_peer as native
from tests.gateway import test_frozen_weixin_media_responses as cdn
from tests.cron import test_strict_weixin_media_fanout as authored


def scope(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(queue, 'DELIVERY_DB', tmp_path / 'cron/deliveries.db')
    monkeypatch.setattr(scheduler, 'load_config', lambda: {'cron': {'wrap_response': False}})


@pytest.fixture(autouse=True)
def isolated_media_root(tmp_path, monkeypatch):
    from gateway.platforms import base
    monkeypatch.setattr(base, "_HERMES_ROOT", tmp_path)
    monkeypatch.setattr(base, "_HERMES_HOME", tmp_path)
    monkeypatch.setattr(base, "MEDIA_DELIVERY_SAFE_ROOTS", (tmp_path / "cache",))

@pytest.mark.asyncio
async def test_done_prefix_does_not_require_current_target_route(tmp_path, monkeypatch):
    scope(tmp_path, monkeypatch)
    await asyncio.to_thread(authored.produce, tmp_path)
    async with native.peer() as wire:
        await authored.consume(tmp_path, wire.base, 'after:0:done')
        prefix_count = len(wire.events)
        async with native.adapter_for('weixin', wire) as (adapter, _):
            adapter.config.enabled = True
            config = GatewayConfig(platforms={Platform.WEIXIN: adapter.config})
            monkeypatch.setattr('gateway.config.load_gateway_config', lambda: config)
            from gateway.profile_routing import ProfileRoute
            suffix_routes = SharedRouteAdapters({Platform.WEIXIN: adapter},
                [ProfileRoute('suffix-route', 'weixin', 'reviewer-profile', chat_id='peer-B')])
            processed = await asyncio.to_thread(scheduler.drain_delivery_queue,
                suffix_routes, asyncio.get_running_loop())
            with queue._transaction() as conn:
                row = dict(conn.execute('SELECT * FROM deliveries').fetchone())
            print('PROBE_DONE_PREFIX ' + json.dumps({'processed': processed, 'status': row['status'],
                'before': prefix_count, 'after': len(wire.events)}))
            assert row['status'] == 'delivered', 'saved done prefix must not be resolved through a now-missing route'
            assert len(wire.events) - prefix_count == 4


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['claim', 'ack'])
async def test_fanout_transaction_rejects_containing_row_identity_drift(tmp_path, monkeypatch, boundary):
    scope(tmp_path, monkeypatch)
    path = tmp_path / 'proof.bin'
    path.write_bytes(b'ORIGINAL')
    execution = 'reviewer-execution'
    job = {'id': 'reviewer-job', 'execution_id': execution}
    content = 'SEALED\nMEDIA:' + str(path)
    row = queue.enqueue(execution, job, content)
    async with native.peer() as wire, native.adapter_for('weixin', wire) as (adapter, chat):
        state = native.state_for('weixin', chat)
        state.profile = str(tmp_path)
        state.execution_id = execution
        state.target = {'platform': 'weixin', 'chat_id': chat, 'thread_id': None}
        state.platform_name = 'weixin'
        state.loop = asyncio.get_running_loop()
        plan = await helper.freeze(adapter, state, [('send_document', {'file_path': str(path)})], [(str(path), False)])
        frozen = {'kind': 'live-http', '_frozen_http_platform': 'weixin', 'plan': plan}
        bundle = stages.prepare_bundle([(state.target, state, '', frozen)], job, content, False)
        claimed = queue.claim_next(execution_id=execution, expected=row, thread_plan=bundle)
        assert claimed is not None
        try:
            if boundary == 'ack':
                assert queue.claim_fanout_target(execution, 0)
                monkeypatch.setattr(dispatch, '_seed_live_delivery_sessions', lambda *a, **k: None)
                receipt = await asyncio.to_thread(stages.send_target, state, frozen)
                assert len(wire.events) == 3
            with queue._transaction() as conn:
                conn.execute('UPDATE deliveries SET content=? WHERE execution_id=?', ('REPLACED SEALED PAYLOAD', execution))
            altered = queue.get_status(execution)
            with pytest.raises(ValueError):
                stages.validate_row(altered)
            accepted = (queue.claim_fanout_target(execution, 0) if boundary == 'claim' else
                stages.transition(execution, 0, 'target_claimed', 'done', receipt))
            print('PROBE_ROW_IDENTITY ' + json.dumps({'boundary': boundary, 'accepted': accepted,
                'wire_count': len(wire.events), 'stage': json.loads(queue.get_status(execution)['thread_json'])['items'][0]['stage']}))
            assert accepted is False, 'containing durable-row mismatch must refuse the existing target CAS'
        finally:
            queue._ACTIVE_DELIVERIES.discard(execution)


@pytest.mark.asyncio
async def test_all_fanout_preflight_checks_actual_frozen_bytes_against_durable_media(tmp_path, monkeypatch):
    from plugins.platforms import frozen_delivery as shared
    scope(tmp_path, monkeypatch)
    await asyncio.to_thread(authored.produce, tmp_path)
    path = tmp_path / 'proof.bin'
    original = shared._read_media
    async with native.peer() as wire, native.adapter_for('weixin', wire) as (adapter, _):
        adapter.config.enabled = True
        monkeypatch.setattr('gateway.config.load_gateway_config', lambda: GatewayConfig(platforms={Platform.WEIXIN: adapter.config}))
        def aba(candidate):
            if Path(candidate) == path:
                data, stat = original(candidate)
                # Snapshot materialization sees B, path has A again before final media_identity.
                return b'B' * len(data), stat
            return original(candidate)
        monkeypatch.setattr(shared, '_read_media', aba)
        processed = await asyncio.to_thread(scheduler.drain_delivery_queue, {Platform.WEIXIN: adapter}, asyncio.get_running_loop())
        with queue._transaction() as conn:
            row = dict(conn.execute('SELECT * FROM deliveries').fetchone())
        assert processed == 0 and row['status'] == 'pending' and row['thread_json'] is None
        assert wire.events == []
        print('PROBE_ACTUAL_MEDIA ' + json.dumps({'processed': processed, 'wire_count': len(wire.events), 'status': row['status']}))


@pytest.mark.asyncio
async def test_lost_ack_fences_fresh_owner_and_both_concurrent_readers(tmp_path, monkeypatch):
    scope(tmp_path, monkeypatch)
    await asyncio.to_thread(authored.produce, tmp_path)
    async with native.peer(lambda e, i: 'lost' if e.path == '/cdn-upload' else None) as wire:
        one, two = await asyncio.gather(authored.consume(tmp_path, wire.base), authored.consume(tmp_path, wire.base))
        before = len(wire.events)
        fresh = await authored.consume(tmp_path, wire.base)
        assert fresh['row']['status'] == 'unknown'
        assert before == len(wire.events) == 3
        assert (tmp_path / 'weixin-media-producer').read_text().splitlines() == ['executed']
        print('PROBE_CONCURRENT_UNKNOWN ' + json.dumps({'first_statuses': [one['row']['status'], two['row']['status']],
            'fresh_status': fresh['row']['status'], 'wire_count': len(wire.events)}))


@pytest.mark.asyncio
async def test_parent_timeout_terminalizes_unknown_stops_suffix_and_recovery(tmp_path, monkeypatch):
    scope(tmp_path, monkeypatch)
    await asyncio.to_thread(authored.produce, tmp_path)
    monkeypatch.setattr(dispatch, '_get_standalone_send_timeout', lambda: 0.1)
    gate = SimpleNamespace(index=1, accepted=asyncio.Event(), release=asyncio.Event())
    async with cdn.cdn_peer(gate=gate) as wire, native.adapter_for('weixin', wire) as (adapter, _):
        adapter.config.enabled = True
        adapter._cdn_base_url = wire.base + '/c2c'
        monkeypatch.setattr('gateway.config.load_gateway_config', lambda: GatewayConfig(platforms={Platform.WEIXIN: adapter.config}))
        complete = asyncio.Event()
        original_send = helper.send
        async def observe(plan):
            try:
                return await original_send(plan)
            finally:
                complete.set()
        monkeypatch.setattr(helper, 'send', observe)
        draining = asyncio.create_task(asyncio.to_thread(scheduler.drain_delivery_queue,
            {Platform.WEIXIN: adapter}, asyncio.get_running_loop()))
        await asyncio.wait_for(gate.accepted.wait(), 10)
        assert await asyncio.wait_for(draining, 10) == 1
        await asyncio.wait_for(complete.wait(), 5)
        gate.release.set()
        with queue._transaction() as conn:
            row = dict(conn.execute('SELECT * FROM deliveries').fetchone())
        before = len(wire.events)
        assert row['status'] == 'unknown' and before == 1
        fresh = await authored.consume(tmp_path, wire.base)
        assert fresh['row']['status'] == 'unknown' and len(wire.events) == before
        print('PROBE_PARENT_TIMEOUT ' + json.dumps({'status': row['status'], 'wire_count': before,
            'fresh_status': fresh['row']['status'], 'producer_lines': len((tmp_path/'weixin-media-producer').read_text().splitlines())}))
