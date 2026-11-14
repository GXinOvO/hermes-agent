from contextlib import nullcontext
from types import SimpleNamespace

import aiohttp
from aiohttp import web
import pytest

from gateway.background_messaging_results import recover
from gateway.config import PlatformConfig
from gateway.platforms.base import SendResult
from gateway.strict_transport import single_attempt_transport
from gateway.platforms.webhook import WebhookAdapter
from hermes_state import SessionDB
from hermes_state_background import begin, complete, pending_deliveries
from tests.gateway.test_strict_queued_settlement import PeerAdapter


class LostAckPeer(PeerAdapter):
    @single_attempt_transport
    async def send(self, chat_id, content, **kwargs):
        try:
            async with self.session.post(self.url, json={'text':content}) as response:
                await response.read()
        except aiohttp.ClientError:
            return SendResult(success=False, error='ACK missing', retryable=True)
        return SendResult(success=False, error='ACK missing', retryable=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('ack', [True, False])
async def test_background_webhook_delegates_only_to_verified_target_once(tmp_path, ack):
    posts = []

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
    try:
        async with aiohttp.ClientSession() as session:
            target = (PeerAdapter if ack else LostAckPeer)(url, session)
            adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={'routes':{}}))
            adapter.gateway_runner = SimpleNamespace(
                _authorization_adapter=lambda platform, profile:target,
                _shared_bot_target_allowed=lambda *args: True,
            )
            adapter._profile_scope = lambda profile:nullcontext()
            adapter._delivery_info['retained'] = {'deliver':'slack', 'deliver_extra':{'chat_id':'chat'}}
            with SessionDB(tmp_path / 'state.db') as db:
                binding = {'profile_id':str(tmp_path), 'parent_session_id':'messaging-background',
                    'event_type':'background.complete', 'source':{'platform':'webhook','chat_id':'retained','chat_type':'dm'}}
                begin(db, 'finished', binding)
                complete(db, 'finished', binding, {'task_id':'finished','text':'sealed answer'})
                runner = SimpleNamespace(_delivery_adapter_for=lambda source:adapter)
                assert await recover(runner, db) == int(ack)
                assert await recover(runner, db) == 0
                assert posts == [{'text':'sealed answer'}]
                assert pending_deliveries(db) == []
    finally:
        await peer.cleanup()


@pytest.mark.asyncio
async def test_unverified_webhook_target_is_refused_before_background_claim(tmp_path):
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={'routes':{}}))
    sends = []
    class UnknownTarget:
        async def send(self, *args, **kwargs):
            sends.append('attempted')
            return SendResult(success=True)
    adapter.gateway_runner = SimpleNamespace(
        _authorization_adapter=lambda platform, profile:UnknownTarget(),
        _shared_bot_target_allowed=lambda *args: True,
    )
    adapter._profile_scope = lambda profile:nullcontext()
    adapter._delivery_info['retained'] = {'deliver':'slack', 'deliver_extra':{'chat_id':'chat'}}
    with SessionDB(tmp_path / 'state.db') as db:
        binding = {'profile_id':str(tmp_path), 'parent_session_id':'messaging-background',
            'event_type':'background.complete', 'source':{'platform':'webhook','chat_id':'retained','chat_type':'dm'}}
        begin(db, 'finished', binding)
        complete(db, 'finished', binding, {'task_id':'finished','text':'sealed answer'})
        runner = SimpleNamespace(_delivery_adapter_for=lambda source:adapter)
        assert await recover(runner, db) == 0
        assert pending_deliveries(db)[0]['delivery'] == 'pending'
        assert sends == []


@pytest.mark.asyncio
async def test_strict_webhook_preflight_refuses_unrouted_target_before_transport(monkeypatch):
    from gateway.platforms import webhook_delivery

    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={'routes':{}}))
    adapter.gateway_runner = SimpleNamespace(
        _authorization_adapter=lambda platform, profile: object(),
        _shared_bot_target_allowed=lambda *args: False,
    )
    adapter._profile_scope = lambda profile:nullcontext()
    prepared = []

    async def should_not_prepare(*args, **kwargs):
        prepared.append((args, kwargs))

    monkeypatch.setattr('gateway.strict_transport.prepare_strict_transport', should_not_prepare)
    with pytest.raises(RuntimeError, match='not routed'):
        await webhook_delivery.prepare_strict_delivery(
            adapter, {'deliver':'slack', 'deliver_extra':{'chat_id':'chat'}}, 'sealed answer')
    assert prepared == []


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['telegram', 'discord', 'slack'])
async def test_webhook_preflights_exact_sdk_content_and_thread(name, tmp_path, monkeypatch):
    from gateway.strict_transport import strict_call, prepare_strict_transport
    from tests.gateway.test_strict_sdk_preflight import connected
    from tests.gateway.test_strict_sdk_delivery import peer
    async with peer() as wire, connected(name, wire, monkeypatch) as (target, chat):
        if name == 'discord':
            import discord
            target._client.get_channel = lambda cid:discord.PartialMessageable(state=target._client._connection, id=cid)
        adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={'routes':{}}))
        adapter.gateway_runner = SimpleNamespace(
            _authorization_adapter=lambda platform, profile:target,
            _shared_bot_target_allowed=lambda *args: True,
        )
        adapter._profile_scope = lambda profile:nullcontext()
        adapter._delivery_info['retained'] = {'deliver':name, 'deliver_extra':{'chat_id':chat, 'thread_id':'42'}}
        await prepare_strict_transport(adapter, 'send', chat_id='retained', content='sealed answer')
        assert wire.events == []
        result = await strict_call(adapter, 'send', chat_id='retained', content='sealed answer')
        assert result.success, result
        assert len(wire.events) == 1
        from urllib.parse import unquote_plus
        assert 'sealed answer' in unquote_plus(wire.events[0]['body'])
        if name == 'telegram':
            assert '42' in wire.events[0]['body']
        elif name == 'discord':
            assert '/42/messages' in wire.events[0]['path']
        else:
            assert '42' in wire.events[0]['body']
