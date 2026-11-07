"""Provider success must include a concrete receipt and every acknowledged chunk."""
import pytest

from gateway.strict_transport import strict_call, strict_transport_scope
from tests.gateway import test_strict_http_peers as peers


@pytest.mark.asyncio
async def test_mattermost_null_id_is_not_ack(monkeypatch):
    from aiohttp import web
    async with peers.http_peer(lambda r, _: web.json_response({'id': None}) if r.method == 'POST' else None) as peer:
        async with peers.adapter_for('mattermost', peer, monkeypatch) as (adapter, chat):
            with strict_transport_scope():
                result = await adapter.send(chat, 'answer')
            assert not result.success and len(peer.records) == 1


@pytest.mark.asyncio
async def test_signal_empty_result_is_not_ack(monkeypatch):
    from aiohttp import web
    async with peers.http_peer(lambda r, _: web.json_response({'result': {}}) if (r.payload or {}).get('method') == 'send' else None) as peer:
        async with peers.adapter_for('signal', peer, monkeypatch) as (adapter, chat):
            with strict_transport_scope():
                result = await adapter.send(chat, 'answer')
            assert not result.success


@pytest.mark.asyncio
async def test_bluebubbles_new_chat_keeps_every_chunk(monkeypatch):
    async with peers.http_peer(lambda r, _: 'empty-chats' if r.path.endswith('/query') else None) as peer:
        async with peers.adapter_for('bluebubbles', peer, monkeypatch) as (adapter, _chat):
            adapter._private_api_enabled = True
            result = await strict_call(adapter, 'send', chat_id='test@inert.invalid', content='first\n\nsecond\n\nthird')
            assert result.success
            sent = [r for r in peer.records if r.path.endswith('/new') or r.path.endswith('/text')]
            assert [r.payload['message'] for r in sent] == ['first', 'second', 'third']
            assert len([r for r in sent if r.path.endswith('/new')]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('strict', [True, False], ids=['strict', 'human'])
async def test_smtp_connect_fallback_policy(strict, monkeypatch):
    from gateway.config import PlatformConfig
    from plugins.platforms.email import adapter as module
    calls = []
    def fail(*args, **kwargs):
        calls.append(args[4])
        raise TimeoutError('connect ACK missing')
    monkeypatch.setattr(module, '_open_smtp', fail)
    adapter = module.EmailAdapter(PlatformConfig(extra={}))
    if strict:
        with strict_transport_scope():
            result = await adapter.send('receiver@inert.invalid', 'answer')
    else:
        result = await adapter.send('receiver@inert.invalid', 'answer')
    assert not result.success and len(calls) == (1 if strict else 2)
