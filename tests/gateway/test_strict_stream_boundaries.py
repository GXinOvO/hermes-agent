import json
from types import SimpleNamespace

import aiohttp
from aiohttp import web
import pytest

from gateway.config import Platform, StreamingConfig
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from gateway.session_delivery_policy import automation_delivery_scope
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
from hermes_state import SessionDB
from tests.gateway.test_strict_queued_settlement import PeerAdapter, admission


class StreamingPeer(PeerAdapter):
    SUPPORTS_NATIVE_STREAMING = True

    def supports_native_streaming(self, chat_id):
        return True

    def supports_draft_streaming(self, chat_type=None, metadata=None, chat_id=None):
        return True

    async def edit_message(self, *args, **kwargs):
        raise AssertionError('strict completion must not edit before settlement')

    async def send_draft(self, *args, **kwargs):
        raise AssertionError('strict completion must not draft before settlement')

    async def send_stream_frame(self, *args, **kwargs):
        raise AssertionError('strict completion must not seed a native stream')


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['auto', 'edit', 'draft'])
async def test_native_stream_buffers_segments_and_never_claims_delivery(tmp_path, transport):
    with SessionDB(tmp_path / 'state.db') as db:
        _, route, row = admission(db)
        source = SessionSource(platform=Platform.SLACK, chat_id='chat', chat_type='dm')
        event = MessageEvent(text='completion', source=source, message_id=row['request_id'], internal=True)
        async with aiohttp.ClientSession() as session:
            adapter = StreamingPeer('http://127.0.0.1:1/unreachable', session)
            async with automation_delivery_scope(SimpleNamespace(db=db), row, event):
                consumer = GatewayStreamConsumer(adapter, 'chat', StreamConsumerConfig(transport=transport, cursor=''))
                consumer.on_delta('first part')
                consumer.on_segment_break()
                consumer.on_commentary('interim commentary')
                consumer.on_tool_progress('tool finished')
                consumer.on_delta('second part')
                consumer.finish('sealed final')
                await consumer.run()
                assert not consumer.final_content_delivered
                assert not consumer.final_response_sent
                assert not consumer.has_durably_delivered_text('sealed final')
                response = {'final_response':'sealed final', 'response_previewed':True}
                runner = object.__new__(GatewayRunner)
                await runner._run_agent_mark_streamed_delivery(response, SimpleNamespace(
                    stream_consumer_holder=[consumer], source=source, session_key=route))
                assert not response.get('already_sent')


@pytest.mark.asyncio
async def test_proxy_sse_is_buffered_without_false_preview_receipt(tmp_path, monkeypatch):
    requests = []

    async def completion(request):
        requests.append(await request.json())
        response = web.StreamResponse(headers={'Content-Type':'text/event-stream'})
        await response.prepare(request)
        delta = json.dumps({'choices':[{'delta':{'content':'sealed proxy final'}}]})
        await response.write(f'data: {delta}\n\ndata: [DONE]\n\n'.encode())
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_post('/v1/chat/completions', completion)
    peer = web.AppRunner(app)
    await peer.setup()
    site = web.TCPSite(peer, '127.0.0.1', 0)
    await site.start()
    url = f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'
    try:
        with SessionDB(tmp_path / 'state.db') as db:
            _, route, row = admission(db)
            source = SessionSource(platform=Platform.SLACK, chat_id='chat', chat_type='dm')
            event = MessageEvent(text='completion', source=source, message_id=row['request_id'], internal=True)
            runner = object.__new__(GatewayRunner)
            runner.config = SimpleNamespace(streaming=StreamingConfig(enabled=True))
            runner._get_proxy_url = lambda:url
            monkeypatch.setattr('gateway.run._load_gateway_config', lambda:{'display':{'streaming':True}})
            monkeypatch.setattr('agent.secret_scope.get_secret_str', lambda name:'')
            async with aiohttp.ClientSession() as session:
                adapter = StreamingPeer(url + '/unexpected-send', session)
                runner._delivery_adapter_for = lambda source:adapter
                async with automation_delivery_scope(SimpleNamespace(db=db), row, event):
                    result = await runner._run_agent_via_proxy('input', '', [], source, 'sid', route)
                    assert result['final_response'] == 'sealed proxy final'
                    assert not result['response_previewed']
            assert len(requests) == 1
    finally:
        await peer.cleanup()


@pytest.mark.asyncio
async def test_strict_automation_does_not_start_streaming_tts(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr('tools.tts_tool._load_tts_config', dict)
    monkeypatch.setattr('gateway.streaming_tts_consumer.StreamingTTSConsumer',
        lambda *args, **kwargs:calls.append('created') or SimpleNamespace(active=False))
    with SessionDB(tmp_path / 'state.db') as db:
        _, _, row = admission(db)
        source = SessionSource(platform=Platform.SLACK, chat_id='chat', chat_type='dm')
        event = MessageEvent(text='completion', source=source, message_id=row['request_id'], internal=True)
        runner = object.__new__(GatewayRunner)
        runner._gateway_loop = None
        runner._delivery_adapter_for = lambda source:SimpleNamespace(_should_auto_tts_for_chat=lambda *args:True)
        holder = [None]
        async with automation_delivery_scope(SimpleNamespace(db=db), row, event):
            runner._run_agent_start_streaming_tts(source, 'voice', {}, holder)
        assert calls == []
        assert holder == [None]
