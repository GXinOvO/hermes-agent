import asyncio
import base64
import copy
import json

import pytest
from cron import delivery_queue as queue, scheduler
from cron import strict_thread_delivery as stages
from gateway.config import GatewayConfig, Platform
from plugins.platforms import frozen_delivery as public
from tests.cron import test_strict_weixin_media_fanout as authored
from tests.gateway import frozen_live_http_peer as native


def resign(plan):
    plan['transport_sha256'] = public.digest(public.encode({k: v for k, v in plan.items() if k != 'transport_sha256'}))


@pytest.fixture(autouse=True)
def isolated_media_root(tmp_path, monkeypatch):
    from gateway.platforms import base
    monkeypatch.setattr(base, "_HERMES_ROOT", tmp_path)
    monkeypatch.setattr(base, "_HERMES_HOME", tmp_path)
    monkeypatch.setattr(base, "MEDIA_DELIVERY_SAFE_ROOTS", (tmp_path / "cache",))

@pytest.mark.asyncio
async def test_saved_binding_schema_aead_and_profile_route_combination_matrix(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(queue, 'DELIVERY_DB', tmp_path/'cron/deliveries.db')
    monkeypatch.setattr(scheduler, 'load_config', lambda: {'cron': {'wrap_response': False}})
    await asyncio.to_thread(authored.produce, tmp_path)
    async with native.peer() as wire:
        await authored.consume(tmp_path, wire.base, 'before:0:target_claimed')
        assert wire.events == []
        with queue._transaction() as conn:
            original = dict(conn.execute('SELECT * FROM deliveries').fetchone())
        saved = json.loads(original['thread_json'])
        changes = ['nonce-length', 'tag-bitflip', 'bool-version', 'algorithm', 'profile-home',
                   'execution', 'target', 'request-header', 'response-origin', 'media-content']
        async with native.adapter_for('weixin', wire) as (adapter, _):
            adapter.config.enabled = True
            monkeypatch.setattr('gateway.config.load_gateway_config', lambda: GatewayConfig(platforms={Platform.WEIXIN: adapter.config}))
            outcomes = []
            for change in changes:
                altered = copy.deepcopy(saved)
                target = altered['binding']['targets'][1]
                plan = target['plan']
                envelope = plan['crypto_context']
                payload = bytearray(base64.b64decode(envelope['ciphertext']))
                payload[-1] ^= 1
                other = str(tmp_path / 'other-profile')
                edits = {
                    'nonce-length': [(envelope, 'nonce', base64.b64encode(b'x' * 11).decode())],
                    'tag-bitflip': [(envelope, 'ciphertext', base64.b64encode(payload).decode())],
                    'bool-version': [(envelope, 'version', True)],
                    'algorithm': [(envelope, 'algorithm', 'AES-128-GCM')],
                    'profile-home': [(plan, 'scope', other), (plan['identity'], 'scope', other),
                                     (plan['identity'], 'home', other)],
                    'execution': [(plan['identity'], 'execution', 'other-execution')],
                    'target': [(plan['identity']['target'], 'chat_id', 'other-destination')],
                    'request-header': [(plan['prepared'][2]['headers'], 'Authorization', 'SYNTHETIC_FORBIDDEN_HEADER')],
                    'response-origin': [(plan['prepared'][1]['response_slot']['origin'], 1, 'elsewhere.invalid')],
                    'media-content': [(plan['media'][0], 2, '0' * 64), (plan['identity']['media'][0], 2, '0' * 64)],
                }
                for record, key, value in edits[change]:
                    record[key] = value
                resign(plan)
                altered['binding_sha256'] = stages.digest(altered['binding'])
                with queue._transaction() as conn:
                    conn.execute("UPDATE deliveries SET status='pending', thread_json=? WHERE execution_id=?",
                        (json.dumps(altered), original['execution_id']))
                processed = await asyncio.to_thread(scheduler.drain_delivery_queue,
                    {Platform.WEIXIN: adapter}, asyncio.get_running_loop())
                current = queue.get_status(original['execution_id'])
                assert processed == 0 and current['status'] == 'pending'
                assert wire.events == []
                assert (tmp_path/'weixin-media-producer').read_text().splitlines() == ['executed']
                outcomes.append({'case': change, 'processed': processed, 'wire_count': len(wire.events)})
            print('PROBE_BINDING_MATRIX ' + json.dumps({'cases': outcomes, 'first_claim_never_reached': True}))


@pytest.mark.asyncio
async def test_suffix_unavailable_route_refuses_entire_first_fanout_claim(tmp_path, monkeypatch):
    from cron.scheduler_preflight import SharedRouteAdapters
    from gateway.profile_routing import ProfileRoute
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(queue, 'DELIVERY_DB', tmp_path/'cron/deliveries.db')
    monkeypatch.setattr(scheduler, 'load_config', lambda: {'cron': {'wrap_response': False}})
    await asyncio.to_thread(authored.produce, tmp_path)
    async with native.peer() as wire, native.adapter_for('weixin', wire) as (adapter, _):
        adapter.config.enabled = True
        monkeypatch.setattr('gateway.config.load_gateway_config', lambda: GatewayConfig(platforms={Platform.WEIXIN: adapter.config}))
        routes = SharedRouteAdapters({Platform.WEIXIN: adapter},
            [ProfileRoute('prefix-only-route', 'weixin', 'reviewer-profile', chat_id='peer-A')])
        processed = await asyncio.to_thread(scheduler.drain_delivery_queue,
            routes, asyncio.get_running_loop())
        with queue._transaction() as conn:
            row = dict(conn.execute('SELECT * FROM deliveries').fetchone())
        assert processed == 0 and row['status'] == 'pending' and row['thread_json'] is None
        assert wire.events == []
        print('PROBE_SUFFIX_PREFLIGHT ' + json.dumps({'processed': processed, 'wire_count': 0, 'saved_plan': False}))
