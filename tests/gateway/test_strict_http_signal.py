"""Native Signal media batches measured at the signal-cli HTTP boundary."""
import pytest

from gateway.strict_transport import strict_call, strict_transport_scope
from tests.gateway import test_strict_http_peers as peers


class NoWaitScheduler:
    """The rate limiter is a clock policy, not a transport substitute."""
    def state(self):
        return {}

    def estimate_wait(self, count):
        return 0

    async def acquire(self, count):
        pass

    async def report_rpc_duration(self, *args):
        pass

    def record(self, *args):
        pass

    def feedback(self, *args):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize('strict', [True, False], ids=['strict', 'human'])
@pytest.mark.parametrize('fault', ['drop', 'missing', 'reject'])
async def test_signal_batch_unknown_ack_stops_followers(strict, fault, tmp_path, monkeypatch):
    from plugins.platforms.signal import adapter as module
    monkeypatch.setattr(module, 'get_scheduler', NoWaitScheduler)
    monkeypatch.setattr(module, 'SIGNAL_MAX_ATTACHMENTS_PER_MSG', 1)
    path = tmp_path / 'image.png'
    path.write_bytes(b'image')
    def fail(record, records):
        sends = [r for r in records if (r.payload or {}).get('method') == 'send']
        return fault if record in sends and len(sends) == 2 else None

    async with peers.http_peer(fail) as peer, peers.adapter_for('signal', peer, monkeypatch) as (adapter, chat):
        images = [(path.as_uri(), '')] * 3
        if strict:
            result = await strict_call(adapter, 'send_multiple_images', chat_id=chat, images=images)
        else:
            result = await adapter.send_multiple_images(chat, images)
        sends = [r for r in peer.records if (r.payload or {}).get('method') == 'send']
        assert result.success is (not strict)
        assert len(sends) == (2 if strict else 4)
        assert all(len(r.payload['params']['attachments']) == 1 for r in sends)


@pytest.mark.asyncio
async def test_signal_success_keeps_every_batch(tmp_path, monkeypatch):
    from plugins.platforms.signal import adapter as module
    monkeypatch.setattr(module, 'get_scheduler', NoWaitScheduler)
    monkeypatch.setattr(module, 'SIGNAL_MAX_ATTACHMENTS_PER_MSG', 2)
    images = []
    for i in range(5):
        path = tmp_path / f'{i}.png'
        path.write_bytes(str(i).encode())
        images.append((path.as_uri(), ''))
    async with peers.http_peer() as peer, peers.adapter_for('signal', peer, monkeypatch) as (adapter, chat):
        result = await strict_call(adapter, 'send_multiple_images', chat_id=chat, images=images)
        assert result.success
        sends = [r for r in peer.records if (r.payload or {}).get('method') == 'send']
        assert [len(r.payload['params']['attachments']) for r in sends] == [2, 2, 1]
        assert [p for r in sends for p in r.payload['params']['attachments']] == [url[7:] for url, _ in images]


@pytest.mark.asyncio
async def test_signal_missing_attachment_aborts_complete_bundle(tmp_path, monkeypatch):
    from plugins.platforms.signal import adapter as module
    monkeypatch.setattr(module, 'get_scheduler', NoWaitScheduler)
    path = tmp_path / 'exists.png'
    path.write_bytes(b'image')
    async with peers.http_peer() as peer, peers.adapter_for('signal', peer, monkeypatch) as (adapter, chat):
        with strict_transport_scope():
            result = await adapter.send_multiple_images(chat, [(path.as_uri(), ''), ((tmp_path / 'missing.png').as_uri(), '')])
        assert not result.success
        assert not any((r.payload or {}).get('method') == 'send' for r in peer.records)
