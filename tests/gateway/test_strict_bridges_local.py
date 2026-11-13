"""WebSocket command receipts and explicit unsupported bridge boundaries."""
import asyncio
from contextlib import asynccontextmanager, suppress
import json
from types import SimpleNamespace

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from gateway.config import PlatformConfig
from gateway.strict_transport import strict_call, strict_transport_scope, supports_strict_transport
from plugins.platforms.simplex.adapter import SimplexAdapter


@asynccontextmanager
async def simplex_peer(fault=None):
    wire = SimpleNamespace(events=[], connected=asyncio.Event(), base='')

    async def receive(ws):
        async for raw in ws:
            command = json.loads(raw)
            wire.events.append(command)
            if fault == 'lost':
                await ws.close()
                return
            response = {'type':'chatCmdError', 'chatError':{'type':'network'}} if fault == 'error' else {
                'type':'newChatItems', 'chatItems':[{'chatItem':{'meta':{'itemId':1}}}]}
            await ws.send(json.dumps({'corrId':command['corrId'], 'resp':response}))

    async with serve(receive, '127.0.0.1', 0) as server:
        url = 'ws://127.0.0.1:' + str(server.sockets[0].getsockname()[1])
        wire.base = url
        adapter = SimplexAdapter(PlatformConfig(enabled=True))
        adapter._strict_ack_timeout = 0.1
        async with connect(url) as ws:
            adapter._ws = ws

            async def listen():
                async for raw in ws:
                    await adapter._handle_event(json.loads(raw))

            listener = asyncio.create_task(listen())
            try:
                yield wire, adapter
            finally:
                listener.cancel()
                with suppress(asyncio.CancelledError):
                    await listener
                print('WIRE_WS ' + json.dumps(wire.events))


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['send', 'send_document', 'send_image_file', 'send_video', 'send_voice'])
@pytest.mark.parametrize('fault', [None, 'lost', 'error'])
async def test_simplex_single_command_ack_no_reconnect_replay(method, fault, tmp_path):
    from PIL import Image
    file = tmp_path / 'asset.png'
    Image.new('RGB', (4, 4), (0, 0, 0)).save(file)
    async with simplex_peer(fault) as (wire, adapter):
        kwargs = {'chat_id':'123'}
        kwargs[{'send':'content', 'send_image_file':'image_path', 'send_video':'video_path',
                'send_voice':'audio_path'}.get(method, 'file_path')] = 'sealed answer' if method == 'send' else str(file)
        result = await strict_call(adapter, method, **kwargs)
        assert result.success is (fault is None)
        assert len(wire.events) == 1
        assert wire.events[0]['cmd'].startswith('/_send @123 json ')
        assert adapter._pending_responses == {}


@pytest.mark.asyncio
async def test_simplex_strict_media_tag_cannot_send_text_then_media(tmp_path):
    file = tmp_path / 'asset.txt'
    file.write_text('local')
    async with simplex_peer() as (wire, adapter):
        result = await strict_call(adapter, chat_id='123', content='answer MEDIA:' + str(file))
        assert not result.success and not wire.events


@pytest.mark.asyncio
async def test_simplex_human_text_remains_fire_and_forget():
    adapter = SimplexAdapter(PlatformConfig(enabled=True))
    assert (await adapter.send('123', 'human text')).success
    with strict_transport_scope():
        assert not (await adapter.send('123', 'sealed answer')).success


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['raft', 'buzz', 'photon', 'irc'])
async def test_bridge_capability_is_method_scoped_and_unqualified_send_does_not_publish(kind):
    import importlib
    names = {'a2a':'A2AAdapter', 'raft':'RaftAdapter', 'buzz':'BuzzAdapter', 'photon':'PhotonAdapter', 'irc':'IRCAdapter'}
    mod = importlib.import_module('plugins.platforms.' + kind + '.adapter')
    adapter = getattr(mod, names[kind])(PlatformConfig(enabled=True))
    # Raft qualifies text only through an issued claim-bound native plan.
    # An unqualified call must stop before it reaches the adapter.
    assert supports_strict_transport(adapter) is (kind == 'raft')
    for method in ['send_typing', 'send_document', 'send_image_file', 'send_video', 'send_voice']:
        assert not supports_strict_transport(adapter, method)
    expected = 'claim-bound plan required' if kind == 'raft' else 'strict_transport_unverified'
    with pytest.raises(RuntimeError, match=expected):
        await strict_call(adapter, chat_id='peer', content='sealed answer')
    with strict_transport_scope():
        result = await adapter.send('peer', 'sealed answer')
        assert not result.success


@pytest.mark.asyncio
async def test_a2a_strict_capability_is_scoped_to_text_publication():
    """A2A advertises strict transport for its qualified method only.

    sealed text publication (durable_tasks prepare/publish) is the qualified path; every other
    method stays unverified, so the old "no strict capability at all" premise no longer holds.
    """
    import importlib
    mod = importlib.import_module('plugins.platforms.a2a.adapter')
    adapter = mod.A2AAdapter(PlatformConfig(enabled=True))
    assert supports_strict_transport(adapter)
    assert not supports_strict_transport(adapter, 'send_typing')
    with pytest.raises(RuntimeError, match='strict_transport_unverified'):
        await strict_call(adapter, 'send_typing', content='sealed answer')
