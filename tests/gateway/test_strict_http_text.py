"""Real HTTP submission counts for strict text chunks and explicit human retries."""
import re

import pytest

from gateway.strict_transport import strict_call, strict_transport_scope
from tests.gateway import test_strict_http_peers as peers

PLATFORMS = ['bluebubbles', 'mattermost', 'ntfy', 'sms', 'whatsapp_cloud', 'signal']
TEXT = '\n\n'.join(c * 40 for c in 'abc')


def small_chunks(name, adapter, monkeypatch):
    if name == 'mattermost':
        monkeypatch.setattr('plugins.platforms.mattermost.adapter.MAX_POST_LENGTH', 64)
    elif name == 'whatsapp_cloud':
        monkeypatch.setattr(adapter, '_outgoing_chunk_limit', lambda: 64)
    else:
        monkeypatch.setattr(adapter, 'MAX_MESSAGE_LENGTH', 64)


def submissions(peer):
    return [r for r in peer.records if r.method == 'POST' and (r.payload or {}).get('method', 'send') == 'send']


def text_from(record, name):
    if name == 'sms':
        from urllib.parse import parse_qs
        return parse_qs(record.body.decode())['Body'][0]
    if name == 'ntfy':
        return record.body.decode()
    if name == 'signal':
        return record.payload['params']['message']
    if name == 'whatsapp_cloud':
        return record.payload['text']['body']
    return record.payload['message']


@pytest.mark.asyncio
@pytest.mark.parametrize('name', PLATFORMS)
@pytest.mark.parametrize('fault', ['drop', 'missing'])
async def test_text_unknown_ack_stops_chunks(name, fault, monkeypatch):
    async with peers.http_peer(lambda r, _: fault if r.method == 'POST' and (r.payload or {}).get('method', 'send') == 'send' else None) as peer:
        async with peers.adapter_for(name, peer, monkeypatch) as (adapter, chat):
            small_chunks(name, adapter, monkeypatch)
            with strict_transport_scope():
                result = await adapter.send(chat, TEXT)
            assert not result.success
            assert len(submissions(peer)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('name', PLATFORMS)
async def test_strict_text_success_preserves_all_chunks(name, monkeypatch):
    async with peers.http_peer() as peer, peers.adapter_for(name, peer, monkeypatch) as (adapter, chat):
        small_chunks(name, adapter, monkeypatch)
        result = await strict_call(adapter, 'send', chat_id=chat, content=TEXT)
        assert result.success
        sent = submissions(peer)
        assert len(sent) == (1 if name == 'ntfy' else 3)
        text = ''.join(re.sub(r'\s*\(\d+/\d+\)$', '', text_from(r, name)) for r in sent)
        expected = TEXT[:64] if name == 'ntfy' else TEXT
        assert re.sub(r'\s', '', text) == re.sub(r'\s', '', expected)


@pytest.mark.asyncio
@pytest.mark.parametrize('name', PLATFORMS)
async def test_explicit_human_retry_still_submits(name, monkeypatch):
    def fault(record, records):
        sent = [r for r in records if r.method == 'POST' and (r.payload or {}).get('method', 'send') == 'send']
        return 'drop' if record in sent and len(sent) == 1 else None

    async with peers.http_peer(fault) as peer:
        async with peers.adapter_for(name, peer, monkeypatch) as (adapter, chat):
            first = await adapter.send(chat, 'answer')
            second = await adapter.send(chat, 'answer')
            assert not first.success and second.success
            assert len(submissions(peer)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['sms', 'mattermost'])
@pytest.mark.parametrize('strict', [True, False], ids=['strict', 'human'])
async def test_post_redirect_does_not_resubmit_in_strict(name, strict, monkeypatch):
    from aiohttp import web
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def redirect_peer():
        records = []

        async def handler(request):
            records.append(await request.read())
            if request.path != '/target':
                raise web.HTTPTemporaryRedirect('/target')
            return web.json_response({'id': 'posted', 'sid': 'SM-posted'})

        app = web.Application()
        app.router.add_post('/{tail:.*}', handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        from types import SimpleNamespace
        try:
            yield SimpleNamespace(url=f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}', records=records)
        finally:
            await runner.cleanup()

    async with redirect_peer() as peer, peers.adapter_for(name, peer, monkeypatch) as (adapter, chat):
        if strict:
            with strict_transport_scope():
                result = await adapter.send(chat, 'answer')
        else:
            result = await adapter.send(chat, 'answer')
        assert result.success is (not strict)
        assert len(peer.records) == (1 if strict else 2)
