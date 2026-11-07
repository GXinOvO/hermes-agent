"""Native queue regressions for claim-bound standalone plans (SOL-P02/P06)."""
import asyncio
import json

import pytest

from cron import delivery_queue as queue, scheduler
from gateway.config import GatewayConfig, Platform, PlatformConfig
from tests.tools import test_strict_standalone_dispatch as fixtures


@pytest.mark.asyncio
@pytest.mark.parametrize('threads', [('777',), ('777', '888')])
async def test_queue_prepares_each_thread_without_overwriting(tmp_path, monkeypatch, threads):
    monkeypatch.setattr(queue, 'DELIVERY_DB', tmp_path / 'deliveries.db')
    monkeypatch.setattr('cron.delivery_outcome.settle_quietly', lambda *args: None)
    monkeypatch.setattr(scheduler, 'load_config', lambda: {'cron': {'wrap_response': False, 'mirror_to_session': False}})
    config = PlatformConfig(enabled=True, token='inert-preclaim')
    monkeypatch.setattr('gateway.config.load_gateway_config', lambda: GatewayConfig(platforms={Platform.SLACK: config}))
    job = {'id': 'thread-job', 'execution_id': 'thread-exec',
           'deliver': ','.join('slack:C12345678:' + thread for thread in threads)}
    queued = queue.enqueue('thread-exec', job, 'sealed multi-thread result')
    assert queued['status'] == 'pending'
    claims = []
    original_claim = queue.claim_next

    def claim(**kwargs):
        row = original_claim(**kwargs)
        if row is not None:
            claims.append(row['execution_id'])
        return row

    monkeypatch.setattr(queue, 'claim_next', claim)
    async with fixtures.peer() as wire:
        fixtures.route(monkeypatch, wire, 'slack')
        count = await asyncio.to_thread(scheduler.drain_delivery_queue, {}, None)
        again = await asyncio.to_thread(scheduler.drain_delivery_queue, {}, None)
        row = queue.get_status('thread-exec')
        print('THREAD_QUEUE ' + json.dumps({'processed': count, 'again': again,
                                            'claims': claims, 'status': row['status'], 'events': wire.events}))
        assert count == 1 and again == 0
        assert claims == ['thread-exec']
        assert row['status'] == 'delivered', row
        assert [event['json']['thread_ts'] for event in wire.events] == list(threads)
        assert all(event['json']['channel'] == 'C12345678' for event in wire.events)


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', ['slack', 'discord', 'telegram'])
@pytest.mark.parametrize('aba', [False, True])
async def test_queue_checks_actual_frozen_media_against_durable_bytes(tmp_path, monkeypatch, platform, aba):
    from pathlib import Path
    monkeypatch.setattr(queue, 'DELIVERY_DB', tmp_path / 'deliveries.db')
    monkeypatch.setattr('cron.delivery_outcome.settle_quietly', lambda *args: None)
    monkeypatch.setattr(scheduler, 'load_config', lambda: {'cron': {'wrap_response': False, 'mirror_to_session': False}})
    config = PlatformConfig(enabled=True, token='inert')
    monkeypatch.setattr('gateway.config.load_gateway_config', lambda: GatewayConfig(platforms={Platform(platform): config}))
    path = tmp_path / 'sealed.txt'
    path.write_bytes(b'A: durable sealed payload')
    content = 'MEDIA: ' + str(path)
    chat = 'C12345678' if platform == 'slack' else '555'
    job = {'id': 'aba-job', 'execution_id': 'aba-exec', 'deliver': platform + ':' + chat}
    expected = json.loads(queue.enqueue('aba-exec', job, content)['media_json'])
    original_read = Path.read_bytes
    frozen_reads = []

    def read(source):
        if source != path:
            return original_read(source)
        if aba:
            source.write_bytes(b'B: transient untrusted payload')
        try:
            value = original_read(source)
            frozen_reads.append(value.decode())
            return value
        finally:
            source.write_bytes(b'A: durable sealed payload')

    monkeypatch.setattr(Path, 'read_bytes', read)
    claims = []
    original_claim = queue.claim_next

    def claim(**kwargs):
        row = original_claim(**kwargs)
        if row is not None:
            claims.append(row['execution_id'])
        return row

    monkeypatch.setattr(queue, 'claim_next', claim)
    async with fixtures.peer() as wire:
        fixtures.route(monkeypatch, wire, platform)
        count = await asyncio.to_thread(scheduler.drain_delivery_queue, {}, None)
        row = queue.get_status('aba-exec')
        print('ABA_QUEUE ' + json.dumps({'platform': platform, 'aba': aba, 'expected': expected,
            'path_after': original_read(path).decode(), 'frozen_reads': frozen_reads,
            'processed': count, 'claims': claims, 'status': row['status'], 'events': wire.events}))
        assert frozen_reads and set(frozen_reads) == ({'B: transient untrusted payload'} if aba else {'A: durable sealed payload'})
        assert original_read(path) == b'A: durable sealed payload'
        assert count == (0 if aba else 1)
        assert claims == ([] if aba else ['aba-exec'])
        assert row['status'] == ('pending' if aba else 'delivered'), row
        if aba:
            assert wire.events == []
            assert json.loads(row['media_json']) == expected and row['content'] == content
        else:
            assert wire.events
            raw = json.dumps(wire.events)
            if platform == 'slack':
                import hashlib
                assert any(event['body_sha256'] == hashlib.sha256(b'A: durable sealed payload').hexdigest() for event in wire.events)
            else:
                assert 'A: durable sealed payload' in raw
            assert 'B: transient untrusted payload' not in raw


@pytest.mark.asyncio
@pytest.mark.parametrize('drift', ['order', 'path', 'voice', 'filename'])
async def test_queue_rejects_frozen_media_metadata_drift_before_claim(tmp_path, monkeypatch, drift):
    from dataclasses import replace
    from tools import send_message_strict as strict
    monkeypatch.setattr(queue, 'DELIVERY_DB', tmp_path / 'deliveries.db')
    monkeypatch.setattr('cron.delivery_outcome.settle_quietly', lambda *args: None)
    monkeypatch.setattr(scheduler, 'load_config', lambda: {'cron': {'wrap_response': False, 'mirror_to_session': False}})
    config = PlatformConfig(enabled=True, token='inert')
    monkeypatch.setattr('gateway.config.load_gateway_config', lambda: GatewayConfig(platforms={Platform.TELEGRAM: config}))
    first, later = tmp_path / 'first.txt', tmp_path / 'later.txt'
    first.write_text('identical bytes')
    later.write_text('identical bytes')
    content = 'MEDIA: ' + str(first) + '\nMEDIA: ' + str(later)
    job = {'id': 'metadata-job', 'execution_id': 'metadata-exec', 'deliver': 'telegram:555'}
    queue.enqueue('metadata-exec', job, content)
    original = strict.media_bytes

    def freeze(media):
        files = original(media)
        if drift == 'order':
            return tuple(reversed(files))
        file = files[0]
        if drift == 'path':
            file = replace(file, path=str(tmp_path / 'different.txt'))
        elif drift == 'voice':
            file = replace(file, voice=not file.voice)
        else:
            file = replace(file, filename='different.txt')
        return (file, *files[1:])

    monkeypatch.setattr(strict, 'media_bytes', freeze)
    claims = []
    original_claim = queue.claim_next

    def claim(**kwargs):
        claims.append(kwargs)
        return original_claim(**kwargs)

    monkeypatch.setattr(queue, 'claim_next', claim)
    async with fixtures.peer() as wire:
        fixtures.route(monkeypatch, wire, 'telegram')
        assert await asyncio.to_thread(scheduler.drain_delivery_queue, {}, None) == 0
        row = queue.get_status('metadata-exec')
        assert row['status'] == 'pending' and row['content'] == content
        assert claims == [] and wire.events == []
        print('MEDIA_METADATA_REFUSAL ' + json.dumps({'drift': drift, 'status': row['status'], 'claims': claims, 'posts': len(wire.events)}))
