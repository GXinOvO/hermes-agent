import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gateway.config import Platform
from gateway.session import SessionSource
from gateway.strict_transport import strict_transport, strict_transport_scope


@pytest.mark.asyncio
async def test_yuanbao_lost_ack_is_not_retried_in_strict_scope():
    from plugins.platforms.yuanbao.adapter import MessageSender
    sender = MessageSender(SimpleNamespace(name='inert'))
    sender._send_msg_body = AsyncMock(side_effect=TimeoutError('lost ACK'))
    with patch('asyncio.sleep', new=AsyncMock()):
        with strict_transport_scope():
            assert not (await sender.send_text_chunk('direct:inert', 'answer')).success
        assert sender._send_msg_body.await_count == 1
        sender._send_msg_body.reset_mock()
        assert not (await sender.send_text_chunk('direct:inert', 'answer')).success
        assert sender._send_msg_body.await_count == 3


@pytest.mark.asyncio
async def test_weixin_lost_ack_is_not_retried_in_strict_scope():
    from plugins.platforms.weixin.adapter import WeixinAdapter
    adapter = object.__new__(WeixinAdapter)
    adapter._send_text_gate = asyncio.Lock()
    adapter._rate_limit_circuit_until = 0.0
    adapter._send_chunk_retries = 2
    adapter._send_chunk_retry_delay_seconds = 0.0
    adapter.platform = Platform.WEIXIN
    adapter._send_session, adapter._base_url, adapter._token = object(), 'http://inert.invalid', 'inert'
    with patch('plugins.platforms.weixin.adapter._send_message', new=AsyncMock(side_effect=TimeoutError('lost ACK'))) as post:
        with strict_transport_scope(), pytest.raises(TimeoutError):
            await adapter._send_text_chunk(chat_id='inert', chunk='answer', context_token=None, client_id='one')
        assert post.await_count == 1
        post.reset_mock()
        with pytest.raises(TimeoutError):
            await adapter._send_text_chunk(chat_id='inert', chunk='answer', context_token=None, client_id='two')
        assert post.await_count == 3


@pytest.mark.asyncio
async def test_signal_partial_bundle_stops_at_first_unknown_ack():
    from plugins.platforms.signal.adapter import SignalAdapter
    from gateway.platforms.base import SendResult
    adapter = object.__new__(SignalAdapter)
    adapter.account = 'inert'
    adapter._stop_typing_indicator = AsyncMock()
    adapter._resolve_image_path = AsyncMock(side_effect=[('/inert/first.png', None, None), ('/inert/second.png', None, None)])
    adapter._with_target = AsyncMock(return_value={'account':'inert'})
    adapter._send_attachment_batch = AsyncMock(return_value=False)
    scheduler = SimpleNamespace(state=dict, estimate_wait=lambda count:0)
    with patch('plugins.platforms.signal.adapter.get_scheduler', return_value=scheduler), \
         patch('plugins.platforms.signal.adapter.SIGNAL_MAX_ATTACHMENTS_PER_MSG', 1):
        with strict_transport_scope():
            result = await adapter.send_multiple_images('inert', [('one',''), ('two','')])
    assert isinstance(result, SendResult) and not result.success
    assert adapter._send_attachment_batch.await_count == 1


@pytest.mark.asyncio
async def test_unverified_adapter_is_refused_before_network_and_capability_is_method_local():
    from gateway.strict_transport import single_attempt_transport, strict_call, supports_strict_transport
    calls = []
    class Audited:
        @single_attempt_transport
        async def send(self, **kwargs):
            calls.append(strict_transport())
            return 'ACK'
    class Override(Audited):
        async def send(self, **kwargs):
            calls.append('unsafe')
    assert supports_strict_transport(Audited(), 'send')
    assert not supports_strict_transport(Override(), 'send')
    assert not supports_strict_transport(Audited(), 'send_document')
    with pytest.raises(RuntimeError, match='strict_transport_unverified'):
        await strict_call(Override(), 'send', chat_id='inert', content='answer')
    assert calls == []
    assert await strict_call(Audited(), 'send', chat_id='inert', content='answer') == 'ACK'
    assert calls == [True] and not strict_transport()


@pytest.mark.asyncio
async def test_messaging_background_requires_verified_transport_before_any_send():
    from gateway.background_messaging_results import deliver
    adapter = SimpleNamespace(send=AsyncMock(), extract_media=lambda text:([], text), extract_images=lambda text:([], text))
    with pytest.raises(RuntimeError, match='strict_transport_unverified'):
        await deliver(adapter, SessionSource(platform=Platform.SLACK, chat_id='inert'), {'text':'sealed'})
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_unverified_media_refuses_the_whole_plan_before_text_send():
    from gateway.background_messaging_results import deliver
    from gateway.strict_transport import single_attempt_transport
    adapter = SimpleNamespace(send=single_attempt_transport(AsyncMock()), send_image=AsyncMock(),
        extract_media=lambda text:([], text), extract_images=lambda text:([('https://inert.invalid/image.png', 'image')], 'text'))
    with pytest.raises(RuntimeError, match='strict_transport_unverified'):
        await deliver(adapter, SessionSource(platform=Platform.SLACK, chat_id='inert'), {'text':'sealed'})
    adapter.send.assert_not_awaited()
    adapter.send_image.assert_not_awaited()


@pytest.mark.asyncio
async def test_unsupported_background_stays_pending_until_verified_transport_available(tmp_path):
    from gateway import background_messaging_results as durable
    from gateway.platforms.base import SendResult
    from gateway.strict_transport import single_attempt_transport
    from hermes_state import SessionDB
    from hermes_state_background import pending_deliveries
    db = SessionDB(tmp_path / 'state.db')
    adapter = SimpleNamespace(send=AsyncMock(return_value=SendResult(success=True)),
        extract_media=lambda text:([], text), extract_images=lambda text:([], text))
    runner = SimpleNamespace(_session_db=db, _delivery_adapter_for=lambda source:adapter)
    source = SessionSource(platform=Platform.SLACK, chat_id='original')
    try:
        context = await durable.prepare(runner, 'work', source, 'bg_transport_pending', [], [])
        await durable.seal(context, 'bg_transport_pending', 'saved')
        assert await durable.recover(runner, db) == 0
        adapter.send.assert_not_awaited()
        assert [(r['task_id'], r['delivery']) for r in pending_deliveries(db)] == [('bg_transport_pending', 'pending')]
        single_attempt_transport(adapter.send)
        assert await durable.recover(runner, db) == 1
        assert await durable.recover(runner, db) == 0
        adapter.send.assert_awaited_once()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_yuanbao_lost_ack_reaches_websocket_only_once():
    from functools import partial
    from plugins.platforms.yuanbao.adapter import ConnectionManager, MessageSender
    from websockets.asyncio.client import connect
    from websockets.asyncio.server import serve
    submissions = []

    async def peer(socket):
        async for message in socket:
            submissions.append(message)
            await socket.close(code=1011, reason='lost ACK')

    adapter = SimpleNamespace(name='inert', _bot_id='', MAX_TEXT_CHUNK=20)
    adapter._connection = ConnectionManager(adapter)
    adapter._outbound = SimpleNamespace(slow_notifier=SimpleNamespace(cancel=lambda chat:None))
    sender = MessageSender(adapter)
    async with serve(peer, '127.0.0.1', 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with connect(f'ws://127.0.0.1:{port}') as socket:
            adapter._connection._ws = socket
            adapter._connection.send_biz_request = partial(adapter._connection.send_biz_request, timeout=0.02)
            with strict_transport_scope():
                result = await sender.send_text('direct:inert', 'first chunk\n\nsecond chunk')
            assert not result.success
    assert len(submissions) == 1


@pytest.mark.asyncio
async def test_weixin_http_ack_failure_stops_after_first_submission():
    import aiohttp
    from aiohttp import web
    from plugins.platforms.weixin.adapter import WeixinAdapter
    submissions = []

    async def peer(request):
        submissions.append(await request.json())
        return web.Response(status=503, text='ACK unavailable')

    app = web.Application()
    app.router.add_post('/ilink/bot/sendmessage', peer)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    adapter = object.__new__(WeixinAdapter)
    adapter._send_text_gate = asyncio.Lock()
    adapter._rate_limit_circuit_until = 0.0
    adapter._send_chunk_retries = 2
    adapter._send_chunk_retry_delay_seconds = 0.0
    adapter.platform = Platform.WEIXIN
    adapter._base_url = f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'
    adapter._token = 'inert'
    try:
        async with aiohttp.ClientSession() as session:
            adapter._send_session = session
            with strict_transport_scope(), pytest.raises(RuntimeError, match='HTTP 503'):
                await adapter._send_text_chunk(chat_id='inert', chunk='sealed', context_token=None, client_id='one')
        assert len(submissions) == 1
        assert submissions[0]['msg']['client_id'] == 'one'
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_signal_http_ack_failure_stops_attachment_batch_and_followers(tmp_path):
    from aiohttp import web
    import httpx
    from plugins.platforms.signal.adapter import SignalAdapter
    submissions = []

    async def peer(request):
        body = await request.json()
        if body['method'] == 'send':
            submissions.append(body)
        return web.Response(status=503, text='ACK unavailable')

    app = web.Application()
    app.router.add_post('/api/v1/rpc', peer)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    adapter = object.__new__(SignalAdapter)
    adapter.account = 'inert'
    adapter.http_url = f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'
    adapter._typing_tasks = {}
    adapter._typing_failures = {}
    adapter._typing_skip_until = {}
    image = tmp_path / 'image.png'
    image.write_bytes(b'inert')
    scheduler = SimpleNamespace(state=dict, estimate_wait=lambda count:0, acquire=AsyncMock(), report_rpc_duration=AsyncMock())
    try:
        async with httpx.AsyncClient() as client:
            adapter.client = client
            with patch('plugins.platforms.signal.adapter.get_scheduler', return_value=scheduler), \
                 patch('plugins.platforms.signal.adapter.SIGNAL_MAX_ATTACHMENTS_PER_MSG', 1), \
                 patch('plugins.platforms.signal.adapter.asyncio.sleep', new=AsyncMock()), strict_transport_scope():
                result = await adapter.send_multiple_images(chat_id='group:inert', images=[(image.as_uri(), ''), (image.as_uri(), '')])
            assert not result.success
            assert len(submissions) == 1
    finally:
        await runner.cleanup()
