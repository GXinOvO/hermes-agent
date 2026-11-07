import asyncio
import aiohttp
from aiohttp import web
import pytest

from cron import delivery_queue as queue, scheduler
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import SendResult
from gateway.strict_transport import single_attempt_transport, strict_transport
from tests.gateway.test_strict_queued_settlement import PeerAdapter


class RetryingPeer(PeerAdapter):
    @single_attempt_transport
    async def send(self, chat_id, content, **kwargs):
        for _ in range(1 if strict_transport() else 3):
            try:
                async with self.session.post(self.url, json={'text':content}) as response:
                    await response.read()
                    return SendResult(success=True, message_id='ack')
            except aiohttp.ClientError:
                continue
        return SendResult(success=False, error='ACK missing', retryable=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('ack', [True, False])
async def test_native_cron_queue_uses_single_attempt_and_never_standalone_fallback(tmp_path, monkeypatch, ack):
    posts, standalone = [], []

    async def receive(request):
        posts.append(await request.json())
        if not ack:
            request.transport.close()
        return web.json_response({'ok':True})

    app = web.Application()
    app.router.add_post('/send', receive)
    peer = web.AppRunner(app)
    await peer.setup()
    site = web.TCPSite(peer, '127.0.0.1', 0)
    await site.start()
    url = f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/send'
    monkeypatch.setattr(queue, 'DELIVERY_DB', tmp_path / 'deliveries.db')
    monkeypatch.setattr('cron.delivery_outcome.settle_quietly', lambda *args:None)
    monkeypatch.setattr(scheduler, 'load_config', lambda:{'cron':{'wrap_response':False}})
    config = GatewayConfig(platforms={Platform.SLACK:PlatformConfig(enabled=True)})
    monkeypatch.setattr('gateway.config.load_gateway_config', lambda:config)
    async def fallback(*args, **kwargs):
        standalone.append('retry')
        return {'success':True}
    monkeypatch.setattr('tools.send_message_tool._send_to_platform', fallback)
    try:
        async with aiohttp.ClientSession() as session:
            adapter = RetryingPeer(url, session)
            queue.enqueue('exec', {'id':'job','execution_id':'exec','deliver':'slack:chat'}, 'sealed final')
            loop = asyncio.get_running_loop()
            assert await asyncio.to_thread(scheduler.drain_delivery_queue, {Platform.SLACK:adapter}, loop) == 1
            assert await asyncio.to_thread(scheduler.drain_delivery_queue, {Platform.SLACK:adapter}, loop) == 0
            assert posts == [{'text':'sealed final'}]
            assert standalone == []
            assert queue.get_status('exec')['status'] == ('delivered' if ack else 'unknown')
    finally:
        await peer.cleanup()


def test_transport_preflight_refusal_does_not_claim_or_block_other_cron_results(tmp_path, monkeypatch):
    monkeypatch.setattr(queue, 'DELIVERY_DB', tmp_path / 'deliveries.db')
    monkeypatch.setattr('cron.delivery_outcome.settle_quietly', lambda *args:None)
    queue.enqueue('unsupported', {'id':'unverified'}, 'retained')
    queue.enqueue('verified', {'id':'verified'}, 'ready')
    calls = []
    def prepare(job, content, failure):
        if job['id'] == 'unverified':
            raise RuntimeError('strict transport unverified')
    def send(job, content, failure):
        calls.append(job['id'])
    assert queue.drain(send, prepare=prepare) == 1
    assert calls == ['verified']
    assert queue.get_status('unsupported')['status'] == 'pending'
    assert queue.get_status('unsupported')['content'] == 'retained'
    assert queue.get_status('verified')['status'] == 'delivered'


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['preflight', 'claimed'])
async def test_media_changed_between_preflight_and_claim_is_never_submitted(tmp_path, monkeypatch, phase):
    from cron.strict_delivery import prepare, deliver
    from gateway.platforms.base import BasePlatformAdapter
    from gateway.strict_transport import single_attempt_transport
    monkeypatch.setattr(queue, 'DELIVERY_DB', tmp_path / 'deliveries.db')
    monkeypatch.setattr('cron.delivery_outcome.settle_quietly', lambda *args:None)
    monkeypatch.setattr(scheduler, 'load_config', lambda:{'cron':{'wrap_response':False}})
    config = GatewayConfig(platforms={Platform.SLACK:PlatformConfig(enabled=True)})
    monkeypatch.setattr('gateway.config.load_gateway_config', lambda:config)
    path = tmp_path / 'report.txt'
    path.write_text('sealed original')
    monkeypatch.setattr(BasePlatformAdapter, 'filter_media_delivery_paths', staticmethod(lambda paths:paths))
    class MediaPeer(PeerAdapter):
        @single_attempt_transport
        async def send_document(self, *args, **kwargs):
            submitted.append(path.read_text())
            return SendResult(success=True, message_id='ack')
    submitted = []
    loop = asyncio.get_running_loop()
    async with aiohttp.ClientSession() as session:
        adapter = MediaPeer('http://127.0.0.1:1/unreachable', session)
        content = f'MEDIA: {path}'
        queue.enqueue('media', {'id':'job','execution_id':'media','deliver':'slack:chat'}, content)
        original = queue.claim_next
        def claim(**kwargs):
            row = original(**kwargs)
            if phase == 'claimed':
                path.write_text('replaced bytes')
            return row
        monkeypatch.setattr(queue, 'claim_next', claim)
        def before_claim(job, text, failure):
            plan = prepare(job, text, failure, adapters={Platform.SLACK:adapter}, loop=loop)
            if phase == 'preflight':
                path.write_text('replaced bytes')
            return plan
        assert await asyncio.to_thread(queue.drain, deliver, prepare=before_claim) == int(phase == 'claimed')
        assert submitted == []
        assert queue.get_status('media')['status'] == ('unknown' if phase == 'claimed' else 'pending')
